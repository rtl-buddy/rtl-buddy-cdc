"""Stage-4 grammar fuzzer (rtl-buddy-cdc#222).

A grammar that emits topologies the hand-authored corpus hasn't seen
— multi-source aggregators, partial-handshake protocols, deep
multi-rate pipelines, gray-coded buses paired with control flags,
reset-sync chains alongside data crossings. Each generated
:class:`tests.fuzz.templates.base.RenderedCase` flows through the
same Yosys / slang / analyzer pipeline the Stage-3 corpus uses,
so a grammar-derived ``.sv`` is just another input to the existing
runner.

Stage-4 foundation surface
~~~~~~~~~~~~~~~~~~~~~~~~~~

This first cut lands the surface (core types + ≥3 productions +
seeded generation), without yet wiring grammar cases into the
3-column coverage report or the cross-frontend differential oracle
— those come in the integration PR.

Public entry points:

- :func:`generate` — render a single :class:`RenderedCase` for a seed.
- :data:`PRODUCTIONS` — the registry of available non-terminals.
- :class:`Production`, :class:`Prediction`, :class:`Fragment`,
  :class:`GenContext`, :class:`ClockDomain`, :class:`Port` — the
  composable grammar surface a future engineer can extend (issue
  #222 done-when criterion 4).

The grammar is *seeded*, mirroring xeno's mutator contract: a fixed
seed always produces the same SV bytes. The test surface in
:mod:`tests.fuzz.test_grammar` pins that reproducibility, so any
nondeterminism creeping into the productions surfaces immediately.
"""

from __future__ import annotations

from .core import (
    ClockDomain,
    Fragment,
    GenContext,
    Port,
    Prediction,
    Production,
    compose,
    generate,
)
from .productions import PRODUCTIONS

__all__ = [
    "PRODUCTIONS",
    "ClockDomain",
    "Fragment",
    "GenContext",
    "Port",
    "Prediction",
    "Production",
    "compose",
    "generate",
]
