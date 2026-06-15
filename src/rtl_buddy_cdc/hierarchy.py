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

from rtl_buddy_cdc.abstract import _instance_clock, summarise_subtree
from rtl_buddy_cdc.netlist import BoundarySummary, Module
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
    """

    instances: int = 0
    summarised: int = 0
    cache_hits: int = 0
    declined: int = 0
    declined_modules: frozenset[str] = field(default_factory=frozenset)
    boundary_modules: frozenset[str] = field(default_factory=frozenset)

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
    seen: dict[tuple[str, str | None], BoundarySummary | None] = {}
    instances = 0
    cache_hits = 0
    declined_modules: set[str] = set()
    boundary_modules: set[str] = set()

    for inst_name, cell in top.cells.items():
        sub = blackboxes.get(cell.type)
        if sub is None:
            continue
        instances += 1
        context = _instance_clock(top, cell, pin_clocks=spec.pin_clocks)
        cache_key = (cell.type, context)
        if cache_key in seen:
            # Identical subtree in an identical clock context: reuse the
            # already-computed (or already-declined) summary.
            cache_hits += 1
            summary = seen[cache_key]
        else:
            summary = summarise_subtree(
                top, cell, sub, spec, pin_clocks=spec.pin_clocks
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
    )
    return out, stats
