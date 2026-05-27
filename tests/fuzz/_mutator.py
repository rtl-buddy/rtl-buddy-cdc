"""xeno mutator adapter — derives mutated fuzz cases from parent templates.

Stage-3 Layer B (rtl-buddy-cdc#221) consumes the standalone
:mod:`rtl_buddy_xeno` package. Each parent :class:`RenderedCase` from
the corpus is fed through :class:`rtl_buddy_xeno.Mutator`, and each
emitted :class:`rtl_buddy_xeno.Mutant` is wrapped into a new
:class:`RenderedCase` with:

- The mutated SV body in ``case.sv``.
- The parent's SDC bytes, unchanged (mutations live in the SV, not
  the constraints).
- A distinct ``top`` / ``case_id`` so the Yosys content-hash cache
  doesn't collide with the parent.
- ``expected`` and ``forbidden`` reflecting the mutant's
  :class:`rtl_buddy_xeno.Prediction`, encoded against the *parent's*
  rendered finding set (the prediction is a delta, not an absolute).

The parent's finding set is computed lazily — see
:func:`iter_mutant_cases`. The encoded ``expected``/``forbidden`` are
intentionally weak (``Op.GE 1`` for added rules, ``Op.ZERO`` for
removed rules) because the prediction guarantees direction-of-change,
not exact counts.

What's actually wired up today
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

xeno v0.0.1 ships two implemented operators in its no-extras install
path:

- ``CLOCK_POLARITY_SWAP`` — regex token swap; predicts CDC-006.
- ``ATTRIBUTE_TOGGLE`` — regex attribute stripper; per-attribute
  predictions (e.g. ``cdc_sync`` → CDC-002/003).

Three more CDC operators (``SYNC_CHAIN_DEPTH_PERTURB``,
``BIT_EXTRACT_PERMUTE``, ``RESET_POLARITY_FLIP``) ship as stubs that
raise :class:`NotImplementedError`. We treat that exception as
"operator not yet available" and silently skip it — same shape the
slang-frontend cache uses for missing optional deps. Once those
operators land in :ref:`rtl-buddy-xeno#2`, the corpus picks them up
without a code change here.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .templates.base import ExpectedFinding, Op, RenderedCase

if TYPE_CHECKING:
    from rtl_buddy_xeno import Mutant


# CDC-side mutation kinds (xeno#2 row 1–5). The FPV-side operators
# (ARITH_FLIP, BIT_OP_FLIP, COND_*, ASSIGN_DROP, PORT_BINDING_SWAP)
# target ``rb mut``'s property-survival oracle, not the CDC rule
# pack, so they stay out of this adapter's scope.
_CDC_KINDS: tuple[str, ...] = (
    "CLOCK_POLARITY_SWAP",
    "ATTRIBUTE_TOGGLE",
    "SYNC_CHAIN_DEPTH_PERTURB",
    "BIT_EXTRACT_PERMUTE",
    "RESET_POLARITY_FLIP",
)


def xeno_available() -> bool:
    """``True`` when :mod:`rtl_buddy_xeno` is importable.

    The fuzz dependency group pins xeno via a GitHub git source under
    a ``python_version >= '3.13'`` marker; on the py3.11 / py3.12 CI
    matrix entries xeno simply isn't installed and the mutant tests
    skip — same pattern :mod:`tests.fuzz.slang_cache` uses.
    """
    try:
        importlib.import_module("rtl_buddy_xeno")
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class MutantCase:
    """A parent corpus case plus one of its xeno-derived mutants.

    Both the wrapped child :class:`RenderedCase` (``case``) and the
    raw :class:`rtl_buddy_xeno.Mutant` (``mutant``) are kept so the
    differential test in :mod:`tests.fuzz.test_mutants` can show the
    user the diff summary and the prediction rationale on failure.
    """

    parent: RenderedCase
    mutant: "Mutant"
    case: RenderedCase


def _kinds(xeno: Any) -> list[Any]:
    """Return the live :class:`MutationKind` values, in source order."""
    return [getattr(xeno.MutationKind, name) for name in _CDC_KINDS]


def _mutant_case_id(parent: RenderedCase, mutant: "Mutant", index: int) -> str:
    """Stable, filesystem-friendly suffix.

    Includes the mutant index so two mutants from the same operator
    on the same parent get distinct ids; the kind value gives the
    user something to grep for; the seed lives only on the
    :class:`Mutant` itself (operator-internal provenance).
    """
    kind_short = mutant.kind.value
    return f"{parent.case_id}__mut_{kind_short}_{index}"


def iter_mutants(
    parent: RenderedCase,
    *,
    count: int = 16,
    seed: int = 0,
) -> Iterator[MutantCase]:
    """Yield :class:`MutantCase`s derived from a single parent case.

    ``count`` bounds the total mutants per parent across all kinds —
    same semantics as :meth:`rtl_buddy_xeno.Mutator.generate`'s
    ``count`` parameter. Sequential scheduling: exhaust each kind in
    declaration order before moving on. The default 16 is roughly
    "every available structural site" for today's two implemented
    operators — short of the rtl-buddy-cdc#221 done-when target of
    ≥10 per template, which needs the three stubbed xeno operators
    to land first (xeno#2).

    Stubbed operators raise :class:`NotImplementedError` when their
    kind is reached; we catch and continue so the live operators
    produce their mutants even while xeno#2 is in progress.
    """
    if not xeno_available():
        return

    xeno = importlib.import_module("rtl_buddy_xeno")
    mutator = xeno.Mutator.from_sv(parent.sv)

    index = 0
    for kind in _kinds(xeno):
        # Drive each operator with its own ``generate(count=count, ...)``
        # call so a single stubbed or extras-gated kind doesn't poison
        # the whole iteration (xeno raises lazily on the operator's
        # first yield). Two error classes we expect:
        #
        # - :class:`NotImplementedError` — true xeno stub (xeno#2);
        #   the operator declaration exists but no body. Becomes a
        #   no-op for this corpus until the stub lands.
        # - :class:`ImportError` — operator implemented but needs an
        #   xeno extra (``[verible]`` / ``[slang]``) that isn't
        #   installed in this env. Same skip semantics — the
        #   ``CLOCK_POLARITY_SWAP`` / ``ATTRIBUTE_TOGGLE`` operators
        #   that don't need extras still produce mutants.
        try:
            for mutant in mutator.generate(kinds=[kind], count=count, seed=seed):
                case = _wrap_mutant(parent, mutant, index)
                yield MutantCase(parent=parent, mutant=mutant, case=case)
                index += 1
        except (NotImplementedError, ImportError):
            continue


def _wrap_mutant(parent: RenderedCase, mutant: "Mutant", index: int) -> RenderedCase:
    """Build a :class:`RenderedCase` for the analyzer to consume.

    The mutated SV body keeps the parent's ``module {top}`` name, so
    we synthesise a distinct ``top`` for the cache key by appending
    the mutant index. The SV is rewritten with that new top in the
    same byte-position the parent had — preserves any structural
    hash on the surrounding source.
    """
    new_top = f"{parent.top}_mut{index}"
    mutated_sv = mutant.sv.replace(parent.top, new_top, 1)
    new_case_id = _mutant_case_id(parent, mutant, index)

    # Encode the prediction's *direction of change* as expected /
    # forbidden assertions. The corpus runner's contract is "absolute
    # finding set" but xeno predictions are *deltas* from the parent —
    # so we only assert the directional invariant (added rule fires
    # at least once; removed rule is silent). The strict per-rule
    # equality check lives in tests/fuzz/test_mutants.py, which
    # consumes the parent's finding set at run time.
    expected_findings = tuple(
        ExpectedFinding(rule_id, Op.GE, 1)
        for rule_id in mutant.prediction.cdc_rules_added
    )
    forbidden_findings = tuple(
        ExpectedFinding(rule_id, Op.ZERO)
        for rule_id in mutant.prediction.cdc_rules_removed
    )

    return RenderedCase(
        template_name=f"mut_{parent.template_name}",
        case_id=new_case_id,
        sv=mutated_sv,
        sdc=parent.sdc,
        top=new_top,
        params={
            **parent.params,
            "mutant_kind": mutant.kind.value,
            "mutant_index": index,
            "parent_top": parent.top,
        },
        expected=expected_findings,
        forbidden=forbidden_findings,
        extra_yosys_passes=parent.extra_yosys_passes,
    )
