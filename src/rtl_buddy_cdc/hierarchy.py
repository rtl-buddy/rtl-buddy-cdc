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

from dataclasses import dataclass, field

from rtl_buddy_cdc.abstract import (
    _instance_clocks,
    summarise_subtree,
    summarise_sync_primitive,
)
from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.netlist import BoundarySummary, Module
from rtl_buddy_cdc.primitives import is_sync_primitive, normalise_type
from rtl_buddy_cdc.sdc import ClockSpec


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
    """

    instances: int = 0
    summarised: int = 0
    cache_hits: int = 0
    declined: int = 0
    declined_modules: frozenset[str] = field(default_factory=frozenset)
    boundary_modules: frozenset[str] = field(default_factory=frozenset)
    primitive_modules: frozenset[str] = field(default_factory=frozenset)

    @property
    def shared_subtree_reused(self) -> bool:
        """True iff at least one ``(module, clock context)`` was
        instantiated more than once and its summary served from cache
        rather than being recomputed."""
        return self.cache_hits > 0


def compose_boundaries(
    top: Module,
    blackboxes: dict[str, Module] | None,
    spec: ClockSpec,
    *,
    max_depth: int = 16,
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
    seen: dict[tuple[str, frozenset[str]], BoundarySummary | None] = {}
    instances = 0
    cache_hits = 0
    declined_modules: set[str] = set()
    boundary_modules: set[str] = set()
    primitive_modules: set[str] = set()

    for inst_name, cell in top.cells.items():
        sub = blackboxes.get(cell.type)
        if sub is None:
            continue
        instances += 1
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
        # Clock CONTEXT is the full frozenset of clock roots the instance
        # carries (FIX 1) — not a single root, so a dual-clock IP keys
        # distinctly from a single-clock one and identical instances still
        # hit the cache under the same root set.
        context = _instance_clocks(
            top, cell, sub, spec=spec, pin_clocks=spec.pin_clocks, max_depth=max_depth
        ).roots
        cache_key = (cell.type, context)
        if cache_key in seen:
            # Identical subtree in an identical clock context: reuse the
            # already-computed (or already-declined) summary.
            cache_hits += 1
            summary = seen[cache_key]
        else:
            summary = summarise_subtree(
                top, cell, sub, spec, pin_clocks=spec.pin_clocks, max_depth=max_depth
            )
            seen[cache_key] = summary
        if summary is None:
            declined_modules.add(cell.type)
        else:
            out[inst_name] = summary
            boundary_modules.add(cell.type)

    stats = CompositionStats(
        instances=instances,
        summarised=len(out),
        cache_hits=cache_hits,
        declined=len(declined_modules),
        declined_modules=frozenset(declined_modules),
        boundary_modules=frozenset(boundary_modules),
        primitive_modules=frozenset(primitive_modules),
    )
    return out, stats


def reconvergence_unsafe_instances(crossings: list[Crossing]) -> set[str]:
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

    Pure: reads only the crossing list.
    """
    ports_by_inst: dict[str, set[str]] = {}
    for c in crossings:
        if c.dst_boundary is None:
            continue
        inst, port = c.dst_boundary
        ports_by_inst.setdefault(inst, set()).add(port)
    return {inst for inst, ports in ports_by_inst.items() if len(ports) >= 2}
