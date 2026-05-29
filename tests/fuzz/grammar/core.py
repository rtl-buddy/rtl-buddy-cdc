"""Grammar core: terminals, productions, composition, top-level driver.

The grammar emits :class:`tests.fuzz.templates.base.RenderedCase`
instances built by *composing* productions: each production emits
a :class:`Fragment` (SV snippets + ports + clocks + a verdict delta);
the driver concatenates the fragments into a single module body,
unions the verdicts, and renders the module + SDC.

The verdict delta has the same shape as xeno's
:class:`rtl_buddy_xeno.Prediction` (Stage-3 Layer B), so a grammar
case's expected finding-set is composable from the productions it
used. The grammar's runner contract is the same as xeno's mutants:
``ExpectedFinding(rule_id, Op.GE, 1)`` for added rules,
``ExpectedFinding(rule_id, Op.ZERO)`` for removed.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from ..templates.base import ExpectedFinding, Op, RenderedCase


@dataclass(frozen=True)
class Prediction:
    """Per-production verdict delta.

    The same ``added``/``removed`` shape xeno's mutator carries —
    so a downstream consumer (the analyzer-differential runner or
    the coverage-steering hook) can reason about grammar
    productions and mutant operators uniformly.
    """

    cdc_rules_added: frozenset[str] = frozenset()
    cdc_rules_removed: frozenset[str] = frozenset()
    rationale: str = ""

    def merge(self, other: "Prediction") -> "Prediction":
        """Union the two deltas; ``removed`` wins over ``added``.

        Composing productions that introduce a finding (e.g. an
        unsynced crossing → CDC-001) and one that silences it (e.g.
        a globally-applied attribute-mark) leaves the silenced rule
        out of the combined ``added`` set. Today's foundation
        productions don't yet use ``removed`` — the union math is
        trivially additive — but the merge contract is fixed so
        future productions (e.g. ``cdc_sync_attribute_blanket``)
        don't have to re-design the composition rule.
        """
        added = (self.cdc_rules_added | other.cdc_rules_added) - (
            self.cdc_rules_removed | other.cdc_rules_removed
        )
        removed = self.cdc_rules_removed | other.cdc_rules_removed
        return Prediction(
            cdc_rules_added=frozenset(added),
            cdc_rules_removed=frozenset(removed),
            rationale=(
                f"{self.rationale}; {other.rationale}".strip("; ")
                if self.rationale or other.rationale
                else ""
            ),
        )


@dataclass(frozen=True)
class ClockDomain:
    """A grammar terminal: a clock with a period (ns).

    The grammar's SDC emitter declares one ``create_clock`` per
    domain and places every pair in the same
    ``set_clock_groups -asynchronous`` clause — Stage-4 productions
    treat any two distinct domains as async-to-each-other, matching
    the hand-authored corpus's SDC convention.
    """

    name: str
    period: float


@dataclass(frozen=True)
class Port:
    """A grammar terminal: a top-level module port.

    ``sampling_clock`` populates the SDC's ``set_input_delay`` for
    input ports — without it, CDC-011 (unconstrained input) fires
    on every grammar-introduced input, which would dominate the
    finding set with a single rule.
    """

    name: str
    direction: str  # "input" | "output"
    width: int = 1
    sampling_clock: str | None = None

    def declaration(self) -> str:
        width_part = "" if self.width == 1 else f" [{self.width - 1}:0]"
        return f"    {self.direction:<6} logic{width_part} {self.name}"


@dataclass
class Fragment:
    """A production's SV emission + verdict delta.

    Fragments are concatenated by :func:`compose`; ports / clocks
    are merged into the module header and SDC respectively.
    Production authors don't see the module-level shape — they
    only declare what they introduce.

    ``extra_yosys_passes`` carries Yosys pass strings the production
    needs between ``flatten`` and ``write_json`` (e.g. ``"opt_dff;"``
    for gated-bus shapes that CDC-012's $dffe detector keys off).
    :func:`compose` unions these across the chosen productions.
    """

    decls: list[str] = field(default_factory=list)
    always_blocks: list[str] = field(default_factory=list)
    assigns: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    clocks: list[ClockDomain] = field(default_factory=list)
    prediction: Prediction = field(default_factory=Prediction)
    extra_yosys_passes: tuple[str, ...] = ()


@dataclass
class GenContext:
    """Per-generate-call mutable state shared across productions.

    The shared :class:`random.Random` is the single source of
    nondeterminism — productions must draw from ``ctx.rng`` and
    never from module-level state. The :meth:`uniq` counter
    guarantees signal-name uniqueness across compositions (two
    sync-chain productions can coexist in one module without
    colliding on ``sync_q``).
    """

    rng: random.Random
    counter: int = 0
    declared_clocks: dict[str, ClockDomain] = field(default_factory=dict)

    def uniq(self, base: str) -> str:
        self.counter += 1
        return f"{base}_{self.counter}"

    def get_or_declare_clock(self, name: str, period: float) -> ClockDomain:
        existing = self.declared_clocks.get(name)
        if existing is not None:
            return existing
        clk = ClockDomain(name=name, period=period)
        self.declared_clocks[name] = clk
        return clk


# A production is a callable that, given the shared context,
# emits a :class:`Fragment`. The pure-function shape lets productions
# stay stateless — the only mutation flows through ``ctx``.
EmitFn = Callable[[GenContext], Fragment]


@dataclass(frozen=True)
class Production:
    """A grammar non-terminal.

    ``declared`` is the *static* verdict the production carries —
    used by the coverage-steering hook (issue #222 Sketch point 5)
    to pick productions that lift under-covered rules. The
    :class:`Fragment` returned by ``emit`` carries the actual
    prediction for the emitted instance, which may differ from
    ``declared`` if the production has parameters that change the
    verdict (e.g. a sync-chain production with a randomly-chosen
    depth).
    """

    name: str
    emit: EmitFn
    declared: Prediction


def compose(productions: list[Production], ctx: GenContext) -> Fragment:
    """Chain productions into a single module-body fragment.

    Each production is emitted independently — there is no
    signal-threading between productions, so a sync-chain followed
    by a comb-source produces two parallel crossing sites in the
    same module, not one chained crossing. This keeps each
    production's verdict locally interpretable.
    """
    out = Fragment()
    extra_passes: list[str] = []
    for prod in productions:
        frag = prod.emit(ctx)
        out.decls.extend(frag.decls)
        out.always_blocks.extend(frag.always_blocks)
        out.assigns.extend(frag.assigns)
        out.ports.extend(frag.ports)
        out.clocks.extend(frag.clocks)
        out.prediction = out.prediction.merge(frag.prediction)
        for p in frag.extra_yosys_passes:
            if p not in extra_passes:
                extra_passes.append(p)
    out.extra_yosys_passes = tuple(extra_passes)
    return out


def _render_sdc(clocks: list[ClockDomain], ports: list[Port]) -> str:
    """Emit the SDC. One ``create_clock`` per declared clock; a single
    ``set_clock_groups -asynchronous`` listing every clock as its own
    group (the grammar treats all clocks as mutually async, matching
    the hand-authored corpus's SDC convention); ``set_input_delay``
    per input port that declared a ``sampling_clock``.
    """
    seen: dict[str, ClockDomain] = {}
    for c in clocks:
        seen.setdefault(c.name, c)
    sorted_clocks = sorted(seen.values(), key=lambda c: c.name)

    lines = [
        f"create_clock -name {c.name} -period {c.period} [get_ports {c.name}]"
        for c in sorted_clocks
    ]
    if len(sorted_clocks) >= 2:
        groups = " ".join(f"-group {{{c.name}}}" for c in sorted_clocks)
        lines.append(f"set_clock_groups -asynchronous {groups}")

    for p in ports:
        if p.direction == "input" and p.sampling_clock is not None:
            lines.append(
                f"set_input_delay -clock {p.sampling_clock} 1.0 [get_ports {p.name}]"
            )
    return "\n".join(lines) + "\n"


def _render_module(top: str, body: Fragment) -> str:
    """Render the module header (ports) + body (decls + always +
    assigns). Clocks are rendered as ``input logic`` ports — the
    grammar uses ``create_clock [get_ports ...]`` in the SDC, so the
    clock identifiers must be top-level inputs.
    """
    clock_ports = [
        Port(name=c.name, direction="input")
        for c in sorted({c.name: c for c in body.clocks}.values(), key=lambda c: c.name)
    ]
    # Preserve insertion order for non-clock ports — production
    # authors can rely on the rendered module's port order matching
    # the order their fragments were composed.
    all_ports = clock_ports + body.ports
    header_lines = [p.declaration() + "," for p in all_ports[:-1]]
    if all_ports:
        header_lines.append(all_ports[-1].declaration())
    header = "\n".join(header_lines)

    body_chunks: list[str] = []
    if body.decls:
        body_chunks.append("\n".join(body.decls))
    if body.always_blocks:
        body_chunks.append("\n\n".join(body.always_blocks))
    if body.assigns:
        body_chunks.append("\n".join(body.assigns))
    body_text = "\n\n".join(body_chunks)

    return f"module {top} (\n{header}\n);\n{body_text}\nendmodule\n"


def generate(
    seed: int,
    *,
    productions: list[Production] | None = None,
    n_productions: int | None = None,
    top: str | None = None,
) -> RenderedCase:
    """Render one grammar-generated :class:`RenderedCase`.

    Deterministic given ``seed`` and ``productions`` — the same
    inputs always yield byte-identical SV / SDC. This is the
    reproducibility property issue #222's Sketch point 4 calls out
    and :mod:`tests.fuzz.test_grammar` pins.

    ``productions`` defaults to the full :data:`PRODUCTIONS`
    registry; the coverage-steering hook (integration PR) passes a
    filtered subset to bias generation toward under-covered rules.
    ``n_productions`` defaults to a random pick from [2, 4] —
    enough composition to exercise non-trivial topologies, bounded
    so a single case stays Yosys-elaboratable in <1 s.
    """
    if productions is None:
        from .productions import PRODUCTIONS as _DEFAULT

        productions = list(_DEFAULT)
    if not productions:
        raise ValueError("generate() needs at least one production")

    rng = random.Random(seed)
    ctx = GenContext(rng=rng)

    n = n_productions if n_productions is not None else rng.randint(2, 4)
    chosen = [rng.choice(productions) for _ in range(n)]

    body = compose(chosen, ctx)

    case_top = top if top is not None else f"fuzz_grammar_seed{seed}"
    sv = _render_module(case_top, body)
    sdc = _render_sdc(body.clocks, body.ports)

    expected = tuple(
        ExpectedFinding(rule_id, Op.GE, 1)
        for rule_id in sorted(body.prediction.cdc_rules_added)
    )
    forbidden = tuple(
        ExpectedFinding(rule_id, Op.ZERO)
        for rule_id in sorted(body.prediction.cdc_rules_removed)
    )

    return RenderedCase(
        template_name="grammar",
        case_id=case_top,
        sv=sv,
        sdc=sdc,
        top=case_top,
        params={
            "seed": seed,
            "productions": [p.name for p in chosen],
            "n_productions": n,
        },
        expected=expected,
        forbidden=forbidden,
        extra_yosys_passes=" ".join(body.extra_yosys_passes),
    )
