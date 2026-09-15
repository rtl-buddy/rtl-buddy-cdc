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
byte span of each logical command, then hands the span back to
``_tokenize`` for the words themselves. A naive ``splitlines()``
would corrupt any corpus SDC using a continuation — today's
templates don't, but the corpus is generated and the grammar pass
(rtl-buddy-cdc#222) renders SDC programmatically, so the mutator
must not silently depend on that.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from rtl_buddy_cdc import sdc as sdc_mod

from ._mutator import encode_prediction
from .templates.base import RenderedCase

#: Operator ids. Strings rather than an enum so they read the same as
#: ``rtl_buddy_xeno.MutationKind`` values do in a ``case_id`` / report.
UNDECLARE_CLOCK_PORT = "UNDECLARE_CLOCK_PORT"
CLOCK_PERIOD_SCALE = "CLOCK_PERIOD_SCALE"

#: Multipliers applied to ``-period`` by :data:`CLOCK_PERIOD_SCALE`.
#: One each side of the parent value so the operator produces both a
#: "this clock got much faster" and a "much slower" variant of every
#: declared clock.
PERIOD_SCALES: tuple[float, ...] = (0.1, 10.0)

_EDGE_RE = re.compile(r"\b(?:pos|neg)edge\s+([A-Za-z_][A-Za-z0-9_$]*)")

# ``-period`` plus its operand. The gap tolerates a backslash-newline
# continuation between the flag and the number (``_tokenize`` collapses
# it to whitespace, so the parser accepts that spelling).
_PERIOD_RE = re.compile(
    r"-period(?P<gap>(?:[ \t]|\\\n)+)(?P<num>[^\s\\\]\}]+)",
)


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
    """One logical SDC command and the byte span it occupies.

    ``text[start:end]`` is the command including any trailing
    end-of-line comment and the newline that terminates it, so
    ``text[:start] + text[end:]`` deletes it cleanly.
    """

    words: tuple[str, ...]
    start: int
    end: int

    @property
    def name(self) -> str:
        return self.words[0] if self.words else ""


def split_commands(text: str) -> list[SdcCommand]:
    """Split an SDC into logical commands, keeping each one's span.

    Structure mirrors :func:`rtl_buddy_cdc.sdc._tokenize` — that
    function is the authority on what a "word" is, and it is called
    here to produce :attr:`SdcCommand.words` from the span — but it
    discards offsets, which a text rewriter needs.
    """
    out: list[SdcCommand] = []
    n = len(text)
    i = 0
    start: int | None = None
    at_word_boundary = True

    def flush(end: int) -> None:
        nonlocal start
        if start is None:
            return
        chunk = text[start:end]
        tokenized = sdc_mod._tokenize(chunk)
        words = tuple(tokenized[0]) if tokenized else ()
        out.append(SdcCommand(words=words, start=start, end=end))
        start = None

    while i < n:
        c = text[i]

        # Line continuation — does not end the command.
        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            i += 2
            at_word_boundary = True
            continue

        if c == "\n":
            flush(i + 1)
            i += 1
            at_word_boundary = True
            continue

        if c in " \t\r":
            i += 1
            at_word_boundary = True
            continue

        # ``#`` at a word boundary comments to end-of-line and (per
        # ``_tokenize``) breaks any continuation. The comment is folded
        # into the command's span so deleting the command takes its
        # trailing comment with it instead of orphaning it.
        if c == "#" and at_word_boundary:
            j = i
            while j < n and text[j] != "\n":
                j += 1
            if j < n:
                j += 1
            flush(j)
            i = j
            at_word_boundary = True
            continue

        if start is None:
            start = i

        if c == "{" and at_word_boundary:
            i = _skip_nested(text, i, "{", "}")
        elif c == "[" and at_word_boundary:
            i = _skip_nested(text, i, "[", "]")
        elif c == '"' and at_word_boundary:
            i = _skip_quoted(text, i)
        else:
            i += 1
        at_word_boundary = False

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
    """Identifiers used as a ``posedge`` / ``negedge`` signal in ``sv``."""
    return frozenset(_EDGE_RE.findall(sv))


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
        chunk = sdc_text[cmd.start : cmd.end]
        match = _PERIOD_RE.search(chunk)
        if match is None:
            continue
        try:
            period = float(match.group("num"))
        except ValueError:
            continue
        name = _command_ports(cmd) or [cmd.name]
        for scale in PERIOD_SCALES:
            scaled = _format_period(period * scale)
            new_chunk = chunk[: match.start("num")] + scaled + chunk[match.end("num") :]
            yield SdcMutant(
                sdc=sdc_text[: cmd.start] + new_chunk + sdc_text[cmd.end :],
                kind=CLOCK_PERIOD_SCALE,
                diff_summary=(
                    f"{', '.join(name)}: -period {match.group('num')} -> {scaled} "
                    f"(x{scale:g})"
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


def _format_period(value: float) -> str:
    """Render a scaled period as a plain decimal.

    ``10.0`` × 0.1 → ``"1.0"``, × 10 → ``"100.0"``; ``0.1`` × 0.1 →
    ``"0.01"``. Fixed-point (never scientific notation) because the SDC
    parser's ``float()`` is fine with either but a human reading the
    diff is not.
    """
    text = f"{value:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


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
    behaviour end-to-end.
    """
    new_top = f"{parent.top}_sdcmut{index}"
    expected, forbidden = encode_prediction(mutant.cdc_rules_added)
    return RenderedCase(
        template_name=f"sdcmut_{parent.template_name}",
        case_id=_sdc_mutant_case_id(parent, mutant, index),
        sv=parent.sv.replace(parent.top, new_top, 1),
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
    """
    del seed
    index = 0
    for mutant in (
        *iter_undeclare_clock_port(parent.sv, parent.sdc),
        *iter_clock_period_scale(parent.sdc),
    ):
        yield SdcMutantCase(
            parent=parent,
            mutant=mutant,
            case=_wrap_sdc_mutant(parent, mutant, index),
        )
        index += 1
