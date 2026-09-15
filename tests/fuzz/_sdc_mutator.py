"""Consumer-side SDC mutator (rtl-buddy-cdc#293).

:mod:`tests.fuzz._mutator` drives ``rtl_buddy_xeno`` over the parent
case's **SystemVerilog** and passes the SDC through untouched. That
leaves two rules structurally unreachable by mutation: CDC-021 (flop
CLK on a port with no ``create_clock``) and CDC-009 (pulse-width
fast→slow) both have their precondition in the *constraints* file,
not in the RTL. The scope decision in rtl-buddy-xeno#16 was that xeno
stays SV-only and SDC mutation is owned here — hence this module.

Both operators are pure text rewrites of the parent's SDC; the SV is
byte-identical apart from the ``module <top>`` rename that keeps the
Yosys content-hash cache from colliding with the parent.

Operators
~~~~~~~~~

``UNDECLARE_CLOCK_PORT``
    Delete one whole ``create_clock`` command that targets a port.
    Any flop whose CLK traces to that port now sits on an undeclared
    clock → CDC-021's precondition. Predicts CDC-021 **positively**
    when the precondition is verifiable from the parent (see
    :func:`_predicts_cdc_021`), conservatively otherwise.

``CLOCK_PERIOD_SCALE``
    Rewrite one ``-period`` operand by ×0.1 and ×10, leaving every
    other byte of the command alone. Prediction is always
    conservative — see :func:`iter_clock_period_scale`.

Command spans, not lines
~~~~~~~~~~~~~~~~~~~~~~~~

:func:`split_commands` mirrors :func:`rtl_buddy_cdc.sdc._tokenize`'s
structure (backslash-newline continuations, ``{...}`` / ``[...]`` /
``"..."`` words, ``#`` comments at a word boundary) but keeps the
byte span of each logical command **and of every word inside it**,
then hands the span back to ``_tokenize`` for the words themselves.
Per-word spans are what lets ``CLOCK_PERIOD_SCALE`` rewrite the real
``-period`` operand instead of the first ``-period``-looking text in
the line (a ``-comment "… -period 99"`` or ``{…}`` operand would
otherwise win). A naive ``splitlines()``
would corrupt any corpus SDC using a continuation — today's
templates don't, but the corpus is generated and the grammar pass
(rtl-buddy-cdc#222) renders SDC programmatically, so the mutator
must not silently depend on that.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from rtl_buddy_cdc import sdc as sdc_mod

from ._mutator import (
    blank_sv_comments_and_strings,
    encode_prediction,
    rename_module_decl,
)
from .templates.base import RenderedCase

#: Operator ids. Strings rather than an enum so they read the same as
#: ``rtl_buddy_xeno.MutationKind`` values do in a ``case_id`` / report.
UNDECLARE_CLOCK_PORT = "UNDECLARE_CLOCK_PORT"
CLOCK_PERIOD_SCALE = "CLOCK_PERIOD_SCALE"

#: Multipliers applied to ``-period`` by :data:`CLOCK_PERIOD_SCALE`.
#: One each side of the parent value so the operator produces both a
#: "this clock got much faster" and a "much slower" variant of every
#: declared clock.
#: :class:`~decimal.Decimal`, not ``float``, so ``0.000001 * 0.1`` is
#: exactly ``1E-7`` and the rendered operand never drifts in the last
#: digits of the diff.
PERIOD_SCALES: tuple[Decimal, ...] = (Decimal("0.1"), Decimal("10"))

_EDGE_RE = re.compile(r"\b(?:pos|neg)edge\s+([A-Za-z_][A-Za-z0-9_$]*)")


@dataclass(frozen=True)
class SdcMutant:
    """One mutated SDC plus its provenance and prediction.

    Deliberately parallel to ``rtl_buddy_xeno.Mutant`` +
    ``rtl_buddy_xeno.Prediction`` flattened into one record: ``kind`` /
    ``diff_summary`` mirror the mutant side, ``rationale`` /
    ``cdc_rules_added`` the prediction side. The directional check in
    :mod:`tests.fuzz.test_sdc_mutants` is therefore the same shape as
    the xeno one in :mod:`tests.fuzz.test_mutants`.

    There is no ``cdc_rules_removed`` field: neither operator can
    *silence* a rule the parent fired in a way this module can verify
    (deleting a ``create_clock`` mostly hides crossings behind the
    ``<unconstrained>`` sentinel, which is a rule-interaction shift,
    not a prediction). Adding the field with a permanently empty value
    would be noise; it can be introduced when an operator earns it.
    """

    sdc: str
    kind: str
    diff_summary: str
    rationale: str
    cdc_rules_added: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class SdcMutantCase:
    """A parent corpus case plus one of its SDC-derived mutants.

    Mirrors :class:`tests.fuzz._mutator.MutantCase` so both mutant
    families can be reported with the same diagnostic block.
    """

    parent: RenderedCase
    mutant: SdcMutant
    case: RenderedCase


@dataclass(frozen=True)
class SdcCommand:
    """One logical SDC command and the byte spans it occupies.

    ``text[start:end]`` is the command including any trailing
    end-of-line comment and the newline that terminates it, so
    ``text[:start] + text[end:]`` deletes it cleanly.

    :attr:`spans` is parallel to :attr:`words`: ``spans[i]`` is the
    ``(lo, hi)`` byte range of ``words[i]`` **in the whole source
    text**, so a rewriter can splice one operand without touching a
    byte of the rest. The span is the word's *source* form, quotes and
    braces included — ``"10"`` spans three bytes, ``words[i]`` is
    ``10`` — because that is what has to be replaced. :attr:`spans` is
    empty when the walk and :func:`rtl_buddy_cdc.sdc._tokenize`
    disagree on the word count (see :func:`split_commands`); callers
    must treat that as "no span information" rather than index into it.
    """

    words: tuple[str, ...]
    start: int
    end: int
    spans: tuple[tuple[int, int], ...] = ()

    @property
    def name(self) -> str:
        return self.words[0] if self.words else ""


def split_commands(text: str) -> list[SdcCommand]:
    """Split an SDC into logical commands, keeping every byte span.

    Structure mirrors :func:`rtl_buddy_cdc.sdc._tokenize` — that
    function is the authority on what a "word" is, and it is called
    here to produce :attr:`SdcCommand.words` from the command's span —
    but it discards offsets, which a text rewriter needs. So the walk
    below reproduces ``_tokenize``'s word boundaries (continuations,
    ``{...}`` / ``[...]`` / ``"..."`` words, ``#`` comments) purely to
    record where each word starts and ends.

    The two are cross-checked: when the walk and ``_tokenize`` disagree
    on how many words a command has, :attr:`SdcCommand.spans` is left
    empty rather than handed out misaligned. A span-consuming caller
    then declines to mutate that command, which is the safe direction.
    """
    out: list[SdcCommand] = []
    n = len(text)
    i = 0
    start: int | None = None
    word_start: int | None = None
    spans: list[tuple[int, int]] = []

    def end_word(pos: int) -> None:
        """Close a bare (unquoted, unbracketed) word ending at ``pos``."""
        nonlocal word_start
        if word_start is not None:
            spans.append((word_start, pos))
            word_start = None

    def flush(end: int) -> None:
        nonlocal start
        end_word(end)
        if start is None:
            spans.clear()
            return
        chunk = text[start:end]
        tokenized = sdc_mod._tokenize(chunk)
        words = tuple(tokenized[0]) if tokenized else ()
        aligned = tuple(spans) if len(spans) == len(words) else ()
        out.append(SdcCommand(words=words, start=start, end=end, spans=aligned))
        spans.clear()
        start = None

    while i < n:
        c = text[i]

        # Line continuation — ends the word, not the command.
        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            end_word(i)
            i += 2
            continue

        if c == "\n":
            end_word(i)
            flush(i + 1)
            i += 1
            continue

        if c in " \t\r":
            end_word(i)
            i += 1
            continue

        # ``#`` at a word boundary comments to end-of-line and (per
        # ``_tokenize``) breaks any continuation. The comment is folded
        # into the command's span so deleting the command takes its
        # trailing comment with it instead of orphaning it.
        if c == "#" and word_start is None:
            j = i
            while j < n and text[j] != "\n":
                j += 1
            if j < n:
                j += 1
            flush(j)
            i = j
            continue

        if start is None:
            start = i

        # ``_tokenize`` only opens a brace / bracket / quoted word when
        # no bare word is in progress (``not word``); ``a"b"`` is one
        # bare word, not two. ``word_start is None`` is that condition.
        if c == "{" and word_start is None:
            j = _skip_nested(text, i, "{", "}")
            spans.append((i, j))
            i = j
        elif c == "[" and word_start is None:
            j = _skip_nested(text, i, "[", "]")
            spans.append((i, j))
            i = j
        elif c == '"' and word_start is None:
            j = _skip_quoted(text, i)
            spans.append((i, j))
            i = j
        else:
            if word_start is None:
                word_start = i
            i += 1

    flush(n)
    return out


def _skip_nested(text: str, i: int, opener: str, closer: str) -> int:
    """Index just past the balanced ``opener``/``closer`` span at ``i``."""
    depth = 1
    i += 1
    n = len(text)
    while i < n and depth > 0:
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
        i += 1
    return i


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the double-quoted word starting at ``i``."""
    n = len(text)
    i += 1
    while i < n and text[i] != '"':
        i += 2 if text[i] == "\\" and i + 1 < n else 1
    return min(i + 1, n)


def _command_ports(cmd: SdcCommand) -> list[str]:
    """Port/pin names a ``create_clock``-family command targets.

    Runs the command's words through the real ``ARG_SPECS`` slicer so
    the bracket (``[get_ports x]``), brace (``{x}``) and bare forms are
    peeled by :func:`rtl_buddy_cdc.sdc._extract_names` rather than by a
    second, drifting implementation here.
    """
    spec = sdc_mod.ARG_SPECS.get(cmd.name)
    if spec is None:
        return []
    parsed = sdc_mod._slice(list(cmd.words[1:]), spec)
    names: list[str] = []
    for word in parsed.tail:
        found, _saw_filter = sdc_mod._extract_names(word)
        names.extend(found)
    return names


def _period_operand_span(cmd: SdcCommand) -> tuple[int, int] | None:
    """Byte span of the real ``-period`` operand, or ``None``.

    Locating ``-period`` with a regex over the command's source text is
    wrong: a ``-comment "scaled -period 99"`` operand (or a braced
    ``{-period 99}``) matches first, and the rewrite then edits the
    comment while the clock's period stays put. So the flag is found at
    the *token* level instead.

    :func:`rtl_buddy_cdc.sdc._slice` stays the authority on which word
    is the operand — it knows ``-comment`` takes one word, and it would
    read ``-period`` as ``-name``'s operand in ``-name -period 10``
    (where the command has no period flag at all, and this returns
    ``None``). Its answer is then matched back to the ``(-period,
    operand)`` word pair to recover the index, and :attr:`SdcCommand.spans`
    turns that index into bytes.
    """
    spec = sdc_mod.ARG_SPECS.get(cmd.name)
    if spec is None or len(cmd.spans) != len(cmd.words):
        return None
    parsed = sdc_mod._slice(list(cmd.words[1:]), spec)
    operand = parsed.first("-period")
    if operand is None:
        return None
    for i in range(1, len(cmd.words) - 1):
        if cmd.words[i] == "-period" and cmd.words[i + 1] == operand:
            return cmd.spans[i + 1]
    return None  # pragma: no cover - _slice found it, so the pair exists


def _clocked_ports(sdc_text: str) -> frozenset[str]:
    """Port names still covered by a clock declaration in ``sdc_text``.

    Mirrors CDC-021's ``declared_clock_ports`` (the union of every
    ``Clock.ports``, generated clocks included — see
    ``_handle_create_generated_clock``), plus ``-source`` operands: a
    port feeding a ``create_generated_clock`` is a declared clock
    source even though the handler doesn't record it in ``ports``.
    Erring wide here only ever makes the CDC-021 claim *more*
    conservative.
    """
    spec = sdc_mod.parse(sdc_text)
    ports = {port for clk in spec.clocks.values() for port in clk.ports}
    for cmd in split_commands(sdc_text):
        if cmd.name != "create_generated_clock":
            continue
        arg_spec = sdc_mod.ARG_SPECS.get(cmd.name)
        if arg_spec is None:  # pragma: no cover - table always has it
            continue
        parsed = sdc_mod._slice(list(cmd.words[1:]), arg_spec)
        source = parsed.first("-source")
        if source is not None:
            names, _ = sdc_mod._extract_names(source)
            ports.update(names)
    return frozenset(ports)


def _edge_signals(sv: str) -> frozenset[str]:
    """Identifiers used as a ``posedge`` / ``negedge`` signal in ``sv``.

    The scan runs over comment- and string-blanked source
    (:func:`tests.fuzz._mutator.blank_sv_comments_and_strings`). Run on
    raw text, a line like ``// no flop uses posedge spare_clk`` would
    make :func:`_predicts_cdc_021` claim CDC-021 for a port nothing
    actually clocks — a false positive in the *prediction*, which is
    exactly the thing the differential test is supposed to catch.
    """
    return frozenset(_EDGE_RE.findall(blank_sv_comments_and_strings(sv)))


def _predicts_cdc_021(port: str, parent_sv: str, mutated_sdc: str) -> bool:
    """Whether removing ``port``'s ``create_clock`` must fire CDC-021.

    Two conditions, both checkable from the parent alone:

    1. ``port`` is used directly as a ``posedge`` / ``negedge`` signal
       in the SV. That is the shape whose flop CLK traces back to the
       port itself, which is what CDC-021 keys on (it compares the
       flop's traced clock *domain* against the top-level port names).
       A clock reaching flops through a mux / ICG / buffer chain
       resolves to an internal net instead, so the rule may legitimately
       stay silent — those stay conservative.
    2. No other ``create_clock`` / ``create_generated_clock`` in the
       *mutated* SDC still names the port.

    Why the sentinel doesn't get in the way: on the analyzer path
    ``sdc.synthesize_unconstrained_inputs`` stamps
    ``UNCONSTRAINED_SENTINEL`` on every untyped input port, so an
    undeclared clock port does get a synthetic ``port_clock`` entry.
    That entry is *not* a ``create_clock``: it never lands in
    ``ClockSpec.clocks``, so CDC-021's ``declared_clock_ports`` stays
    empty for the port. And ``run_all`` builds its rule context
    **without** ``clock_for_port``, so ``ctx.domains`` holds the raw
    traced root (the port name) rather than the sentinel — the
    ``module.ports.get(domain)`` lookup in ``check_cdc_021`` therefore
    still resolves. The synthesis suppresses CDC-001/-002/-006 on the
    affected crossings (they skip the sentinel) but not CDC-021.
    """
    if port not in _edge_signals(parent_sv):
        return False
    return port not in _clocked_ports(mutated_sdc)


def iter_undeclare_clock_port(parent_sv: str, sdc_text: str) -> Iterator[SdcMutant]:
    """Yield one mutant per port-targeting ``create_clock`` command.

    The whole command is removed — flags, operand, trailing comment and
    the newline — so the mutated SDC is exactly the parent minus that
    declaration.
    """
    commands = split_commands(sdc_text)
    for cmd in commands:
        if cmd.name != "create_clock":
            continue
        ports = _command_ports(cmd)
        if not ports:
            continue
        mutated = sdc_text[: cmd.start] + sdc_text[cmd.end :]
        removed = sdc_text[cmd.start : cmd.end].strip()
        port_list = ", ".join(ports)
        claimed = frozenset(
            {"CDC-021"}
            if any(_predicts_cdc_021(p, parent_sv, mutated) for p in ports)
            else ()
        )
        if claimed:
            rationale = (
                f"CDC-021: removing this create_clock leaves port(s) "
                f"{port_list} undeclared; the port is used directly as a "
                f"posedge/negedge signal in the parent SV and no other "
                f"clock declaration names it, so every flop it clocks "
                f"lands on an undeclared top-level port."
            )
        else:
            rationale = (
                f"CDC-021 (unverified): removing this create_clock drops the "
                f"declaration for port(s) {port_list}, but either the port "
                f"doesn't clock a flop directly (it reaches CLK through a "
                f"mux / gate / buffer, so the flop's traced domain is an "
                f"internal net, not the port) or another clock declaration "
                f"still names it. No positive claim."
            )
        yield SdcMutant(
            sdc=mutated,
            kind=UNDECLARE_CLOCK_PORT,
            diff_summary=f"-{removed}",
            rationale=rationale,
            cdc_rules_added=claimed,
        )


def iter_clock_period_scale(sdc_text: str) -> Iterator[SdcMutant]:
    """Yield ×0.1 and ×10 ``-period`` variants of every ``create_clock``.

    Only the numeric operand's bytes change; the rest of the command —
    flag spelling, spacing, comment, operand form — is preserved.

    The prediction is conservative on purpose. CDC-009 needs a
    *fast→slow* ratio **on an existing single-bit crossing whose source
    flop carries the edge-detector D-pin shape*; scaling a period only
    moves the ratio. Whether the rescaled clock is the source of such a
    crossing (rather than its destination, or not part of one at all)
    is a whole-netlist question this text rewriter cannot answer, so
    the rationale names CDC-009 and the claim set stays empty.
    """
    for cmd in split_commands(sdc_text):
        if cmd.name != "create_clock":
            continue
        span = _period_operand_span(cmd)
        if span is None:
            continue
        lo, hi = span
        raw = sdc_text[lo:hi]
        try:
            period = Decimal(raw)
        except InvalidOperation:
            # Braced / quoted / expression operands land here and are
            # left alone — the operand's source bytes are not a number.
            continue
        name = _command_ports(cmd) or [cmd.name]
        for scale in PERIOD_SCALES:
            scaled = _format_period(period * scale)
            yield SdcMutant(
                sdc=sdc_text[:lo] + scaled + sdc_text[hi:],
                kind=CLOCK_PERIOD_SCALE,
                diff_summary=(
                    f"{', '.join(name)}: -period {raw} -> {scaled} (x{scale:g})"
                ),
                rationale=(
                    f"CDC-009 (unverified): scaling this clock's period by "
                    f"{scale:g} changes the fast→slow ratio against the other "
                    f"declared clocks. That ratio is only a pulse-width hazard "
                    f"when the rescaled clock is the *source* of a single-bit "
                    f"crossing into a slower domain and the source flop has the "
                    f"edge-detector D-pin shape — neither is verified here, so "
                    f"no positive claim."
                ),
            )


def _format_period(value: Decimal) -> str:
    """Render a scaled period as a plain decimal.

    ``10`` × 0.1 → ``"1.0"``, × 10 → ``"100.0"``; ``2.5`` × 0.1 →
    ``"0.25"``; ``0.000001`` × 0.1 → ``"0.0000001"``.

    :class:`~decimal.Decimal` throughout, with **no precision cap**:
    the previous ``f"{value:.6f}"`` silently rendered any period below
    ``5e-7`` as ``"0.000000"`` → ``"0.0"``, i.e. an
    ``-period 0`` mutant that is a division-by-zero waiting to happen
    in a ratio rule rather than the "much faster clock" the operator
    means. ``normalize()`` drops the trailing zeros multiplication
    introduces and ``format(..., "f")`` keeps the result fixed-point
    (``normalize()`` alone yields ``1E+2`` for ``100``) — the SDC
    parser's ``float()`` accepts scientific notation but a human
    reading the diff should not have to. At least one fractional digit
    is kept for readability, so an integral result reads ``"100.0"``.
    """
    text = format(value.normalize(), "f")
    return f"{text}.0" if "." not in text else text


def _sdc_mutant_case_id(parent: RenderedCase, mutant: SdcMutant, index: int) -> str:
    """Stable, filesystem-friendly suffix — mirrors ``_mutant_case_id``."""
    return f"{parent.case_id}__sdcmut_{mutant.kind}_{index}"


def _wrap_sdc_mutant(
    parent: RenderedCase, mutant: SdcMutant, index: int
) -> RenderedCase:
    """Build the :class:`RenderedCase` the analyzer consumes.

    Cache-key note (the hazard rtl-buddy-cdc#293 called out):
    :attr:`RenderedCase.content_hash` already folds in the SDC bytes
    alongside the SV, the top name and the extra Yosys passes, so an
    SDC-only mutant *does* get its own digest and
    :func:`tests.fuzz.runner._analyze` writes its own
    ``<digest>.sdc``. The ``top`` rename below — same shape as
    :func:`tests.fuzz._mutator._wrap_mutant` — is kept anyway so the
    case is distinguishable in the cache directory and in pytest ids,
    and so the distinctness doesn't silently depend on one line of
    ``content_hash``. ``tests/fuzz/test_sdc_mutants.py`` pins the
    behaviour end-to-end. Only the ``module <top>`` declaration is
    renamed (:func:`tests.fuzz._mutator.rename_module_decl`), so a
    comment or an earlier identifier holding the top name survives.
    """
    new_top = f"{parent.top}_sdcmut{index}"
    expected, forbidden = encode_prediction(mutant.cdc_rules_added)
    return RenderedCase(
        template_name=f"sdcmut_{parent.template_name}",
        case_id=_sdc_mutant_case_id(parent, mutant, index),
        sv=rename_module_decl(parent.sv, parent.top, new_top),
        sdc=mutant.sdc,
        top=new_top,
        params={
            **parent.params,
            "sdc_mutant_kind": mutant.kind,
            "sdc_mutant_index": index,
            "parent_top": parent.top,
        },
        expected=expected,
        forbidden=forbidden,
        extra_yosys_passes=parent.extra_yosys_passes,
    )


def iter_sdc_mutants(parent: RenderedCase, *, seed: int = 0) -> Iterator[SdcMutantCase]:
    """Yield every SDC mutant of ``parent``, operator by operator.

    ``seed`` is accepted for signature parity with
    :func:`tests.fuzz._mutator.iter_mutants` and ignored: both
    operators enumerate their sites exhaustively and deterministically,
    so there is nothing to randomise. A parent with no ``create_clock``
    (the CDC-021 sentinel template ``gap_g10`` ships an empty SDC by
    design) yields nothing.

    The operator iterators are *chained*, not star-expanded: a caller
    that stops early (``next(...)``, a ``-k`` filter, a budget) never
    pays for the mutants it does not look at, and only one mutated SDC
    is materialised at a time. ``index`` still counts across operators
    in yield order, so every case keeps its stable id and its distinct
    ``top``.
    """
    del seed
    chained = itertools.chain(
        iter_undeclare_clock_port(parent.sv, parent.sdc),
        iter_clock_period_scale(parent.sdc),
    )
    for index, mutant in enumerate(chained):
        yield SdcMutantCase(
            parent=parent,
            mutant=mutant,
            case=_wrap_sdc_mutant(parent, mutant, index),
        )
