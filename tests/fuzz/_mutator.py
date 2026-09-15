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

xeno 0.2.0 (PyPI) implements every CDC-side kind this adapter
enumerates in :data:`_CDC_KINDS`:

- ``CLOCK_POLARITY_SWAP`` / ``ATTRIBUTE_TOGGLE`` — parser-free regex
  rewrites (no extras).
- ``SYNC_CHAIN_DEPTH_PERTURB`` / ``CHAIN_STAGE_INSERT`` /
  ``COMB_BETWEEN_STAGES`` / ``BIT_EXTRACT_PERMUTE`` /
  ``RESET_POLARITY_FLIP`` / ``RESET_FANIN_MERGE`` — Verible-CST
  operators (the ``[verible]`` extra), some with optional pyslang
  confidence flagging (``[slang]``).

The three newest (``CHAIN_STAGE_INSERT``, ``COMB_BETWEEN_STAGES``,
``RESET_FANIN_MERGE``) were added for the coverage gaps this repo's
report surfaced in rtl-buddy-cdc#230 (CDC-018 / CDC-014 / RDC-005).
A kind that raises :class:`NotImplementedError` is still treated as
"operator not yet available" and silently skipped — same shape the
slang-frontend cache uses for missing optional deps — so a future
xeno kind can be listed here before it ships.

SDC mutation lives next door
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

xeno is **SV-only** and this adapter passes the parent's SDC through
untouched — that was the explicit scope decision in
rtl-buddy-xeno#16. The two rules whose precondition lives in the
constraints file instead of the RTL (CDC-021, flop CLK on a port with
no ``create_clock``; CDC-009, pulse-width fast→slow) are therefore
unreachable from here. They are covered by the consumer-side
operators in :mod:`tests.fuzz._sdc_mutator`
(``UNDECLARE_CLOCK_PORT`` / ``CLOCK_PERIOD_SCALE``,
rtl-buddy-cdc#293), which mutate the SDC text and keep the SV fixed.
Both families feed the same ``mutants`` column of
:mod:`tests.fuzz.coverage` and share
:func:`encode_prediction` below, so a prediction means the same thing
on either side.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .templates.base import ExpectedFinding, Op, RenderedCase

if TYPE_CHECKING:
    from rtl_buddy_xeno import Mutant


# CDC-side mutation kinds (xeno#2 rows 1-5 plus the xeno#13 / #14 /
# #15 operators from the rtl-buddy-cdc#230 gap analysis). The FPV-side operators
# (ARITH_FLIP, BIT_OP_FLIP, COND_*, ASSIGN_DROP, PORT_BINDING_SWAP)
# target ``rb mut``'s property-survival oracle, not the CDC rule
# pack, so they stay out of this adapter's scope.
_CDC_KINDS: tuple[str, ...] = (
    "CLOCK_POLARITY_SWAP",
    "ATTRIBUTE_TOGGLE",
    "SYNC_CHAIN_DEPTH_PERTURB",
    "CHAIN_STAGE_INSERT",
    "COMB_BETWEEN_STAGES",
    "BIT_EXTRACT_PERMUTE",
    "RESET_POLARITY_FLIP",
    "RESET_FANIN_MERGE",
)


def xeno_available() -> bool:
    """``True`` when :mod:`rtl_buddy_xeno` is importable.

    The fuzz dependency group resolves xeno from PyPI (>=0.2.0). Jobs
    that don't install that group (the matrix ``pytest (with slang)``
    entries) leave xeno absent and the mutant tests skip — same pattern
    :mod:`tests.fuzz.slang_cache` uses.
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


def encode_prediction(
    rules_added: Iterable[str],
    rules_removed: Iterable[str] = (),
) -> tuple[tuple[ExpectedFinding, ...], tuple[ExpectedFinding, ...]]:
    """Encode a prediction's *direction of change* as expected/forbidden.

    The corpus runner's contract is an absolute finding set, but a
    mutant prediction is a *delta* from the parent — so only the
    directional invariant is asserted: an added rule fires at least
    once (``Op.GE 1``), a removed rule stays silent (``Op.ZERO``). The
    strict per-rule check lives in the differential tests, which
    consume the parent's finding set at run time.

    Shared with :mod:`tests.fuzz._sdc_mutator` (rtl-buddy-cdc#293) so
    the SV-side and SDC-side mutant families encode a claim
    identically.
    """
    expected = tuple(ExpectedFinding(rule_id, Op.GE, 1) for rule_id in rules_added)
    forbidden = tuple(ExpectedFinding(rule_id, Op.ZERO) for rule_id in rules_removed)
    return expected, forbidden


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

    ``count`` is a **per-kind** budget: each kind in :data:`_CDC_KINDS`
    gets its own :meth:`rtl_buddy_xeno.Mutator.generate` call with
    ``count=count``, so a parent yields at most
    ``len(_CDC_KINDS) * count`` mutants (kinds are exhausted in
    declaration order). Per-kind rather than per-parent so one
    site-rich kind cannot starve the others, and so a kind that
    raises on its first yield is skipped without poisoning the rest
    — see the exception handling below. In practice every kind
    exhausts its sites well under the default 16 on the corpus
    parents.

    A kind that raises :class:`NotImplementedError` (a xeno stub) or
    an extras/tool-availability error is skipped so the live kinds
    still produce their mutants.
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
        # first yield). Three error classes we expect:
        #
        # - :class:`NotImplementedError` — true xeno stub (xeno#2);
        #   the operator declaration exists but no body. Becomes a
        #   no-op for this corpus until the stub lands.
        # - :class:`ImportError` — operator implemented but needs an
        #   xeno extra (``[verible]`` / ``[slang]``) that isn't
        #   installed in this env. Same skip semantics — the
        #   ``CLOCK_POLARITY_SWAP`` / ``ATTRIBUTE_TOGGLE`` operators
        #   that don't need extras still produce mutants.
        # - :class:`rtl_buddy_view.frontend.verible.VeribleUnavailable`
        #   — verible binary not on PATH. Same skip semantics. The
        #   fuzz CI job installs a pinned Verible so the six CST
        #   operators actually run there; a dev box without Verible
        #   sees only the two regex operators' mutants.
        try:
            for mutant in mutator.generate(kinds=[kind], count=count, seed=seed):
                case = _wrap_mutant(parent, mutant, index)
                yield MutantCase(parent=parent, mutant=mutant, case=case)
                index += 1
        except (NotImplementedError, ImportError):
            continue
        except Exception as exc:  # noqa: BLE001 - extras-gated raises various
            # ``rtl_buddy_view.frontend.verible.VeribleUnavailable`` and
            # any future extras-gated "tool missing" exception bubble
            # up here. Match by exception class name so we don't pull
            # in the optional import just to spell the type.
            if type(exc).__name__ in {
                "VeribleUnavailable",
                "SlangUnavailable",
            }:
                continue
            raise


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

    expected_findings, forbidden_findings = encode_prediction(
        mutant.prediction.cdc_rules_added,
        mutant.prediction.cdc_rules_removed,
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
