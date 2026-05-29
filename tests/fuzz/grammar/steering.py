"""Coverage steering: pick productions that lift under-covered rules.

Stage-4 issue rtl-buddy-cdc#222 Sketch point 5 — closes the loop
between corpus growth and coverage gain. The 3-column coverage
report (Stage-3 Layer C) identifies under-covered rules; this
module maps those rules back to the productions that declare
them in :attr:`Production.declared.cdc_rules_added`, so the
caller can bias :func:`tests.fuzz.grammar.generate` toward
productions that *should* lift the under-covered rule.

The steering picker is intentionally *static*: it consults the
production's declared verdict, not its observed firing pattern.
A production whose declared rule never actually fires is a bug
in the production (its rendered SV doesn't trigger what its
verdict claims) and surfaces via the directional check in
:mod:`tests.fuzz.test_grammar`, not via steering.

Mechanics
~~~~~~~~~

- :func:`productions_lifting` — given a set of rule ids, return
  the productions that declare at least one of them. Used by
  callers that already know which rules they want to exercise.
- :func:`under_covered_rules` — given a per-rule fire counter and
  a threshold, return the rules whose fire count is below it.
  The coverage report uses this to identify steering targets
  before invoking :func:`productions_lifting`.
"""

from __future__ import annotations

from collections.abc import Mapping

from .core import Production
from .productions import PRODUCTIONS


def productions_lifting(
    rule_ids: set[str] | frozenset[str],
    *,
    productions: tuple[Production, ...] | list[Production] = PRODUCTIONS,
) -> list[Production]:
    """Return productions whose declared verdict intersects ``rule_ids``.

    Order is preserved from the input ``productions`` registry so
    the resulting bias-set is deterministic for a fixed registry.
    A production is included if *any* of its declared added rules
    overlaps ``rule_ids`` — even multi-rule productions like a
    future handshake-with-missing-ack (CDC-001 + CDC-012) get
    picked for either target.
    """
    target = frozenset(rule_ids)
    return [p for p in productions if p.declared.cdc_rules_added & target]


def under_covered_rules(
    fires: Mapping[str, int],
    *,
    threshold: int,
    rule_universe: set[str] | frozenset[str] | None = None,
) -> set[str]:
    """Return rules whose fire count is strictly below ``threshold``.

    ``rule_universe`` defaults to the keys present in ``fires`` —
    pass the full :data:`rtl_buddy_cdc.rules.RULES` set when you
    want zero-fire rules to surface too (they don't appear as
    keys in ``fires`` unless the report seeded them at zero).
    """
    if rule_universe is None:
        rule_universe = set(fires)
    return {r for r in rule_universe if fires.get(r, 0) < threshold}
