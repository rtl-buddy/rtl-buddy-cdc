"""Compositional boundary walk (CDC scaling phase 3, #257).

Phases 1/2 made a blackboxed subtree a first-class, loadable boundary
cell (#255) and taught the summariser to abstract a single-clock
subtree to its port boundary (#256). What was still implicit is the
*compositional* contract the epic (#253) is actually about: a block
instantiated N times must be **analysed once**, its boundary summary
cached by module identity and re-applied to every instance, so the
full flattened graph is never materialised.

:func:`compose_boundaries` makes that explicit. It walks the parent's
boundary instances, summarises each *distinct* blackbox module exactly
once (keyed by module name — the cell ``type`` every instance shares),
and returns the boundary map :func:`~rtl_buddy_cdc.domain.find_crossings`
consumes plus a :class:`CompositionStats` record that *proves* the
sharing (``instances`` ≥ ``summarised`` whenever a module is reused).

Pure orchestration: the per-subtree detection / summarisation lives in
:mod:`rtl_buddy_cdc.abstract`; this module only walks instances and
caches results. The file I/O that loads the blackbox siblings stays in
``cli.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rtl_buddy_cdc.abstract import summarise_subtree
from rtl_buddy_cdc.netlist import BoundarySummary, Module
from rtl_buddy_cdc.sdc import ClockSpec


@dataclass(frozen=True)
class CompositionStats:
    """Bookkeeping for one compositional boundary walk.

    ``instances`` is the number of blackbox *instances* the parent
    carries; ``summarised`` is the number of *distinct* blackbox
    modules a :class:`BoundarySummary` was built for. ``cache_hits``
    counts instances whose module had already been summarised — the
    direct evidence that a shared subtree was analysed once and reused
    (``cache_hits == instances - distinct_modules_seen``). ``declined``
    counts distinct modules the summariser refused to abstract (not
    provably single-clock, or a foreign-domain data input it can't
    seed); those instances fall through to the normal flat walk.
    """

    instances: int = 0
    summarised: int = 0
    cache_hits: int = 0
    declined: int = 0
    declined_modules: frozenset[str] = field(default_factory=frozenset)

    @property
    def shared_subtree_reused(self) -> bool:
        """True iff at least one blackbox module was instantiated more
        than once and its summary served from cache rather than being
        recomputed."""
        return self.cache_hits > 0


def compose_boundaries(
    top: Module,
    blackboxes: dict[str, Module] | None,
    spec: ClockSpec,
) -> tuple[dict[str, BoundarySummary], CompositionStats]:
    """Summarise every distinct blackbox subtree once and compose.

    Walks each boundary instance in ``top`` (an ordinary cell whose
    ``type`` is a blackbox module name), summarising the *module* the
    first time it is seen and serving every later instance of the same
    module from the cache — so a block instantiated N times costs one
    :func:`~rtl_buddy_cdc.abstract.summarise_subtree` call, not N. The
    flattened internals of the subtree never exist in ``top`` to begin
    with; the returned :class:`BoundarySummary` map lets
    :func:`~rtl_buddy_cdc.domain.find_crossings` re-seed each instance's
    boundary crossings in their place.

    Returns ``(boundaries, stats)``. ``boundaries`` is keyed by module
    name (matching each instance cell's ``type``) and only contains
    modules that were *provably* single-clock — a module the summariser
    declined is absent (its instances are then analysed by the normal
    flat walk, which sees no internals so reports nothing through the
    opaque boundary). ``stats`` records the instance/cache accounting
    the parity tests assert against.

    Pure: no I/O, no mutation of ``top`` or ``blackboxes``.
    """
    out: dict[str, BoundarySummary] = {}
    if not blackboxes:
        return out, CompositionStats()

    # First-summary cache, keyed by module identity (the cell ``type``).
    # ``None`` records a module we summarised and *declined* so a later
    # instance of the same declined module is also a cache hit (analysed
    # once), not a re-summarise.
    seen: dict[str, BoundarySummary | None] = {}
    instances = 0
    cache_hits = 0
    declined_modules: set[str] = set()

    for cell in top.cells.values():
        sub = blackboxes.get(cell.type)
        if sub is None:
            continue
        instances += 1
        if cell.type in seen:
            # Shared subtree: the module was already summarised (or
            # declined) for an earlier instance — reuse, don't recompute.
            cache_hits += 1
            continue
        summary = summarise_subtree(top, cell, sub, spec, pin_clocks=spec.pin_clocks)
        seen[cell.type] = summary
        if summary is None:
            declined_modules.add(cell.type)
        else:
            out[cell.type] = summary

    stats = CompositionStats(
        instances=instances,
        summarised=len(out),
        cache_hits=cache_hits,
        declined=len(declined_modules),
        declined_modules=frozenset(declined_modules),
    )
    return out, stats
