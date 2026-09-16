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

On top of the first-order pass, :func:`iter_mutants` runs one
**second-order (compound)** pass: every ``CHAIN_STAGE_INSERT``
mutant is re-fed to the mutator and the operator is applied a second
time *to the stage the first pass just inserted*. One insertion on a
2-deep parent chain yields 3 stages — one short of CDC-018's
``depth_threshold`` of 4 — so the rule only ever saw mutant coverage
from parents that already carried a 4-stage chain. The second
insertion closes that gap. Compounding is deliberately narrow: one
compound mutant per first-order insertion, same kind, same chain, so
the mutant count grows additively rather than combinatorially. No
other kind and no other site is compounded.

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
import re
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

    ``compound`` marks the second-order cases built by
    :func:`iter_mutants`' compound pass (a ``CHAIN_STAGE_INSERT``
    re-applied to its own inserted stage). Consumers treat them like
    any other mutant — the flag exists so the coverage report can
    show the split and the tests can select them.
    """

    parent: RenderedCase
    mutant: "Mutant"
    case: RenderedCase
    compound: bool = False


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


# ---- SystemVerilog lexical helpers (shared with ``_sdc_mutator``) -----------

#: The ``module <top>`` declaration header. ``\s+`` rather than a single
#: space because :func:`blank_sv_comments_and_strings` turns an
#: interposed comment into spaces, and ``\b`` on both ends so
#: ``module chip_v2`` is not matched when renaming ``chip``.
_MODULE_DECL_TMPL = r"\bmodule\s+{top}\b"


def blank_sv_comments_and_strings(sv: str) -> str:
    """Return ``sv`` with comments and string literals blanked out.

    Same length as the input and newline-preserving, so an offset into
    the result is an offset into the original source: callers scan the
    blanked text and splice the real one. Handles ``//`` line comments,
    ``/* */`` block comments (multi-line included) and ``"..."`` string
    literals with backslash escapes.

    A character loop rather than a regex: a regex-only stripper has to
    choose which construct wins when they nest (a ``//`` inside a
    string, a quote inside a block comment) and gets one of them wrong.
    Here the first opener encountered simply consumes to its own
    terminator, which is what a lexer does.
    """
    out = list(sv)
    n = len(sv)

    def blank(lo: int, hi: int) -> None:
        for k in range(lo, hi):
            if out[k] != "\n":
                out[k] = " "

    i = 0
    while i < n:
        c = sv[i]
        if c == "/" and i + 1 < n and sv[i + 1] == "/":
            j = i
            while j < n and sv[j] != "\n":
                j += 1
            blank(i, j)
            i = j
        elif c == "/" and i + 1 < n and sv[i + 1] == "*":
            j = i + 2
            while j + 1 < n and not (sv[j] == "*" and sv[j + 1] == "/"):
                j += 1
            j = min(j + 2, n)
            blank(i, j)
            i = j
        elif c == '"':
            j = i + 1
            while j < n and sv[j] != '"':
                j += 2 if sv[j] == "\\" and j + 1 < n else 1
            j = min(j + 1, n)
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def rename_module_decl(sv: str, top: str, new_top: str) -> str:
    r"""Rename the ``module <top>`` declaration — and only that.

    A plain ``sv.replace(top, new_top, 1)`` renames whichever
    occurrence comes first in the file, which is the *comment* in
    sources like ``// top module is chip\nmodule chip (...)``, or an
    earlier identifier that happens to contain the top name. The
    declaration is located instead by an anchored ``\bmodule\s+<top>\b``
    match over the comment-/string-blanked source (offsets are
    preserved, so the match offset indexes the real text) and only that
    span is spliced.

    Falls back to the old ``replace(..., 1)`` behaviour when no
    declaration is found — a mutated body that no longer contains a
    parseable header still needs *some* distinct top for the Yosys
    content-hash cache, and the caller has no better answer.
    """
    scrubbed = blank_sv_comments_and_strings(sv)
    match = re.search(_MODULE_DECL_TMPL.format(top=re.escape(top)), scrubbed)
    if match is None:
        return sv.replace(top, new_top, 1)
    start = match.end() - len(top)
    return sv[:start] + new_top + sv[start + len(top) :]


def _kinds(xeno: Any) -> list[Any]:
    """Return the live :class:`MutationKind` values, in source order."""
    return [getattr(xeno.MutationKind, name) for name in _CDC_KINDS]


def _mutant_case_id(
    parent: RenderedCase,
    mutant: "Mutant",
    index: int,
    *,
    compound: bool = False,
) -> str:
    """Stable, filesystem-friendly suffix.

    Includes the mutant index so two mutants from the same operator
    on the same parent get distinct ids; the kind value gives the
    user something to grep for; the seed lives only on the
    :class:`Mutant` itself (operator-internal provenance). Compound
    (second-order) cases carry a trailing ``_x2`` so they're greppable
    as a family.
    """
    kind_short = mutant.kind.value
    suffix = _COMPOUND_SUFFIX if compound else ""
    return f"{parent.case_id}__mut_{kind_short}_{index}{suffix}"


# ---- second-order (compound) CHAIN_STAGE_INSERT ----------------------------

#: Marker appended to a compound mutant's ``case_id`` / ``top``.
_COMPOUND_SUFFIX = "_x2"

#: ``CHAIN_STAGE_INSERT``'s ``diff_summary`` shape, e.g.
#: ``line 14: insert sync stage `sync_meta_xeno_stage_1` after `sync_meta```.
#: The inserted register's name is what the compound pass re-targets;
#: xeno names it ``<lhs>_xeno_stage_<n>`` via ``_chain_helpers
#: .fresh_identifier``, stripping any existing ``_xeno_stage_<digits>``
#: off the base first — so a second application walks
#: ``q_xeno_stage_1`` → ``q_xeno_stage_2`` instead of compounding the
#: suffix, and the two stage names stay distinct.
_INSERT_SUMMARY_RE = re.compile(
    r"insert sync stage `(?P<inserted>\w+)` after `(?P<lhs>\w+)`"
)


def _extras_skip(exc: BaseException) -> bool:
    """``True`` for an extras/tool-availability raise we treat as a skip.

    Matched by exception class *name* so the adapter doesn't import the
    optional package just to spell the type — see the commentary in
    :func:`iter_mutants`.
    """
    return type(exc).__name__ in {"VeribleUnavailable", "SlangUnavailable"}


def _compound_prediction(xeno: Any, first: "Mutant", second: "Mutant") -> Any:
    """Combine two ``CHAIN_STAGE_INSERT`` predictions into one.

    ``cdc_rules_added`` stays **empty** on purpose. Two insertions do
    take a 2-deep chain to CDC-018's 4-stage threshold, but neither
    the operator nor this adapter can verify that the chain in
    question is a *cross-domain synchroniser* (the rule only counts
    chains whose head is a crossing's destination flop, with a flop —
    not a port or a comb expression — on the source side). Claiming
    CDC-018 here would over-fail the directional check on every
    reset-tree or comb-sourced chain; the coverage report observes
    what actually fires instead. ``perturbs_signals`` is a union —
    both stages' Qs now reach their reader a clock later — and the
    rationale records the depth claim in prose.
    """
    return xeno.Prediction(
        rationale=(
            f"{first.prediction.rationale}. Compound pass: the operator was "
            f"then re-applied to the stage it had just inserted, so this "
            f"mutant deepens the chain by two stages in total. CDC-018 "
            f"(cascaded synchroniser) fires when that brings a synchroniser "
            f"chain up to its >=4-stage threshold; cdc_rules_added stays "
            f"empty because the harness cannot verify from the rewrite "
            f"alone that this chain is a cross-domain synchroniser"
        ),
        cdc_rules_added=frozenset(),
        cdc_rules_removed=(
            first.prediction.cdc_rules_removed | second.prediction.cdc_rules_removed
        ),
        perturbs_signals=(
            first.prediction.perturbs_signals | second.prediction.perturbs_signals
        ),
        perturbs_liveness=(
            first.prediction.perturbs_liveness or second.prediction.perturbs_liveness
        ),
    )


def compound_chain_insert(
    xeno: Any,
    first: "Mutant",
    *,
    count: int = 16,
    seed: int = 0,
) -> "Mutant | None":
    """Re-apply ``CHAIN_STAGE_INSERT`` to the stage ``first`` inserted.

    ``None`` when the first mutant's ``diff_summary`` isn't parseable
    or when the second pass offers no site on the inserted stage (the
    inserted register is the chain tail with no further reader, say).
    Otherwise exactly one :class:`rtl_buddy_xeno.Mutant`: same kind,
    the twice-mutated SV, and a ``diff_summary`` of
    ``<first> ; then <second>``.

    Site selection is by name, not by position: the second pass
    shuffles its site order internally, so the only reliable way to
    pick "the stage we just added" is to match the summary's trailing
    ``after `<inserted>``` against the register the first pass named.
    Any other site would be a *different* chain (or a different point
    on this one), which is the combinatorial blow-up this pass exists
    to avoid.
    """
    match = _INSERT_SUMMARY_RE.search(first.diff_summary)
    if match is None:
        return None
    inserted = match.group("inserted")
    want = f"after `{inserted}`"
    kind = xeno.MutationKind.CHAIN_STAGE_INSERT
    try:
        candidates = list(
            xeno.Mutator.from_sv(first.sv).generate(
                kinds=[kind], count=count, seed=seed
            )
        )
    except (NotImplementedError, ImportError):
        return None
    except Exception as exc:  # noqa: BLE001 - extras-gated raises various
        if _extras_skip(exc):
            return None
        raise
    for second in candidates:
        if not second.diff_summary.endswith(want):
            continue
        return xeno.Mutant(
            sv=second.sv,
            diff_summary=f"{first.diff_summary} ; then {second.diff_summary}",
            seed=second.seed,
            prediction=_compound_prediction(xeno, first, second),
            kind=kind,
        )
    return None


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

    **Compound mutants are extra.** After the per-kind loop, a
    second-order pass re-applies ``CHAIN_STAGE_INSERT`` to the stage
    each first-order ``CHAIN_STAGE_INSERT`` mutant inserted — see
    :func:`compound_chain_insert`. These are *not* drawn from any
    kind's ``count`` budget: the pass yields **at most one compound
    mutant per first-order insertion**, so the ceiling rises from
    ``len(_CDC_KINDS) * count`` to ``len(_CDC_KINDS) * count +
    <number of first-order insertions>``. They arrive last, after
    every first-order mutant, and carry ``MutantCase.compound=True``.

    Why it exists: one insertion on a 2-deep chain gives 3 stages,
    one short of CDC-018's ``depth_threshold`` of 4. The second
    insertion reaches it, which is the only way most corpus parents
    can give CDC-018 mutant coverage at all.
    """
    if not xeno_available():
        return

    xeno = importlib.import_module("rtl_buddy_xeno")
    mutator = xeno.Mutator.from_sv(parent.sv)
    insert_kind = xeno.MutationKind.CHAIN_STAGE_INSERT
    first_order_inserts: list["Mutant"] = []

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
                if mutant.kind is insert_kind:
                    first_order_inserts.append(mutant)
                index += 1
        except (NotImplementedError, ImportError):
            continue
        except Exception as exc:  # noqa: BLE001 - extras-gated raises various
            # ``rtl_buddy_view.frontend.verible.VeribleUnavailable`` and
            # any future extras-gated "tool missing" exception bubble
            # up here. Match by exception class name so we don't pull
            # in the optional import just to spell the type.
            if _extras_skip(exc):
                continue
            raise

    # Second-order pass: one compound mutant per first-order insertion.
    # Runs after the whole first-order loop so a parent's mutant stream
    # is "every first-order mutant, then its compounds" — stable ids
    # for ``pytest -k`` and a stable Yosys cache key ordering.
    for first in first_order_inserts:
        compound = compound_chain_insert(xeno, first, count=count, seed=seed)
        if compound is None:
            continue
        case = _wrap_mutant(parent, compound, index, compound=True)
        yield MutantCase(parent=parent, mutant=compound, case=case, compound=True)
        index += 1


def _wrap_mutant(
    parent: RenderedCase,
    mutant: "Mutant",
    index: int,
    *,
    compound: bool = False,
) -> RenderedCase:
    """Build a :class:`RenderedCase` for the analyzer to consume.

    The mutated SV body keeps the parent's ``module {top}`` name, so
    we synthesise a distinct ``top`` for the cache key by appending
    the mutant index. Only the ``module <top>`` declaration is
    rewritten — see :func:`rename_module_decl` — so a comment or an
    earlier identifier containing the top name is left alone.

    ``compound`` marks a second-order case: the synthesised ``top``
    and ``case_id`` both gain an ``_x2`` tail so a compound mutant is
    identifiable from a cache path, a pytest id or a Yosys log line
    without consulting ``params``.
    """
    new_top = f"{parent.top}_mut{index}" + (_COMPOUND_SUFFIX if compound else "")
    mutated_sv = rename_module_decl(mutant.sv, parent.top, new_top)
    new_case_id = _mutant_case_id(parent, mutant, index, compound=compound)

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
            "mutant_compound": compound,
            "parent_top": parent.top,
        },
        expected=expected_findings,
        forbidden=forbidden_findings,
        extra_yosys_passes=parent.extra_yosys_passes,
    )
