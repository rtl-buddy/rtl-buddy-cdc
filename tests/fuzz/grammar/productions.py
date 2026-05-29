"""Foundation production set for the Stage-4 grammar fuzzer.

Each production emits an *independent* crossing site within the
module — productions don't thread signals between each other, so a
case with two productions becomes a module with two parallel
crossings, each independently exercising its declared rules.

Foundation set covers four of the six non-terminals issue #222
calls out:

- **sync chain** — :func:`_emit_clean_sync_chain` (depth=2 reference)
  and :func:`_emit_unsynced_single_bit` (depth=0 negative).
- **comb source** — :func:`_emit_comb_source` (CDC-006 site).
- **gray counter** — :func:`_emit_gray_counter_crossing`
  (CDC-004-clean wide crossing via ``(* cdc_gray *)``).
- **reset-sync chain** — :func:`_emit_missing_reset_sync` (RDC-001
  site: foreign-domain async reset on a dst-domain flop's ARST pin).

The two remaining non-terminals (``handshake req/ack pair``,
``FIFO read/write skeleton``) land in the integration PR alongside
the coverage-steering hook — they need the steering loop to argue
their additional Yosys / slang elaboration cost is paying for new
coverage rather than redundancy with the hand-authored templates.

Two reference clock domains
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The foundation productions share a two-domain reference
(``src_clk`` + ``dst_clk``) for simplicity. The :class:`GenContext`
already supports declaring additional domains, so future
multi-rate-pipeline / multi-source-aggregator productions can
introduce a third or fourth clock without changing the core.
"""

from __future__ import annotations

from .core import (
    ClockDomain,
    Fragment,
    GenContext,
    Port,
    Prediction,
    Production,
)

# Reference clock periods. The grammar picks one of these pairs per
# generated case so the SDC introduces variety analogous to the
# Stage-3 Layer A sweep — same SV elaboration cost, distinct Yosys
# cache buckets per (sv, sdc) digest.
_PERIOD_PAIRS: tuple[tuple[float, float], ...] = (
    (10.0, 7.5),
    (5.0, 13.0),
    (12.0, 18.5),
    (8.0, 11.0),
    (15.5, 6.0),
    (9.5, 22.0),
)


def _pick_clocks(ctx: GenContext) -> tuple[ClockDomain, ClockDomain]:
    """Pick (src, dst) clocks for a production, declaring them in
    ``ctx`` if first seen so multiple productions share the same
    period choices across a single case.
    """
    if "src_clk" in ctx.declared_clocks and "dst_clk" in ctx.declared_clocks:
        return ctx.declared_clocks["src_clk"], ctx.declared_clocks["dst_clk"]
    src_period, dst_period = ctx.rng.choice(_PERIOD_PAIRS)
    src = ctx.get_or_declare_clock("src_clk", src_period)
    dst = ctx.get_or_declare_clock("dst_clk", dst_period)
    return src, dst


def _emit_clean_sync_chain(ctx: GenContext) -> Fragment:
    """Textbook 2FF synchroniser on a single bit.

    Same shape as ``GoodTwoFF`` in the hand-authored corpus — the
    grammar's "no false positives" sentinel. Verdict carries no
    added rules: a clean chain shouldn't fire anything.
    """
    src, dst = _pick_clocks(ctx)
    suffix = ctx.uniq("clean")
    d_in = f"d_in_{suffix}"
    q_out = f"q_out_{suffix}"
    src_q = f"src_q_{suffix}"
    sync_m = f"sync_m_{suffix}"
    sync_q = f"sync_q_{suffix}"

    decls = [
        f"    logic {src_q};",
        f"    logic {sync_m}, {sync_q};",
    ]
    always = [
        f"    always_ff @(posedge {src.name}) {src_q} <= {d_in};",
        (
            f"    always_ff @(posedge {dst.name}) begin\n"
            f"        {sync_m} <= {src_q};\n"
            f"        {sync_q} <= {sync_m};\n"
            f"    end"
        ),
    ]
    assigns = [f"    assign {q_out} = {sync_q};"]
    return Fragment(
        decls=decls,
        always_blocks=always,
        assigns=assigns,
        ports=[
            Port(name=d_in, direction="input", sampling_clock=src.name),
            Port(name=q_out, direction="output"),
        ],
        clocks=[src, dst],
        prediction=Prediction(rationale="clean 2FF synchroniser; no added findings"),
    )


def _emit_unsynced_single_bit(ctx: GenContext) -> Fragment:
    """Direct src→dst flop with no sync chain. CDC-001 fires.

    Depth=0 means CDC-002 stays silent (CDC-001 owns the finding
    when no chain exists at all — see :mod:`tests.fuzz.templates.cdc001`
    for the same partition).
    """
    src, dst = _pick_clocks(ctx)
    suffix = ctx.uniq("unsync")
    d_in = f"d_in_{suffix}"
    q_out = f"q_out_{suffix}"
    src_q = f"src_q_{suffix}"
    dst_q = f"dst_q_{suffix}"

    decls = [
        f"    logic {src_q};",
        f"    logic {dst_q};",
    ]
    always = [
        f"    always_ff @(posedge {src.name}) {src_q} <= {d_in};",
        f"    always_ff @(posedge {dst.name}) {dst_q} <= {src_q};",
    ]
    assigns = [f"    assign {q_out} = {dst_q};"]
    return Fragment(
        decls=decls,
        always_blocks=always,
        assigns=assigns,
        ports=[
            Port(name=d_in, direction="input", sampling_clock=src.name),
            Port(name=q_out, direction="output"),
        ],
        clocks=[src, dst],
        prediction=Prediction(
            cdc_rules_added=frozenset({"CDC-001"}),
            rationale="single-bit src→dst with no sync chain",
        ),
    )


def _emit_comb_source(ctx: GenContext) -> Fragment:
    """Top-level input feeds the sync chain without a registering
    flop in the source domain. CDC-006 fires on the unregistered
    combinational source.
    """
    src, dst = _pick_clocks(ctx)
    suffix = ctx.uniq("comb")
    a_in = f"a_in_{suffix}"
    b_in = f"b_in_{suffix}"
    q_out = f"q_out_{suffix}"
    comb_w = f"comb_w_{suffix}"
    sync_m = f"sync_m_{suffix}"
    sync_q = f"sync_q_{suffix}"
    src_dummy = f"src_dummy_{suffix}"

    decls = [
        f"    wire {comb_w} = {a_in} & {b_in};",
        f"    logic {sync_m}, {sync_q};",
        f"    logic {src_dummy};",
    ]
    always = [
        (
            f"    always_ff @(posedge {dst.name}) begin\n"
            f"        {sync_m} <= {comb_w};\n"
            f"        {sync_q} <= {sync_m};\n"
            f"    end"
        ),
        # Give src_clk at least one consumer so the analyzer treats
        # it as a real clock domain — same trick CombSource uses.
        f"    always_ff @(posedge {src.name}) {src_dummy} <= 1'b0;",
    ]
    assigns = [f"    assign {q_out} = {sync_q} | {src_dummy};"]
    return Fragment(
        decls=decls,
        always_blocks=always,
        assigns=assigns,
        ports=[
            Port(name=a_in, direction="input", sampling_clock=src.name),
            Port(name=b_in, direction="input", sampling_clock=src.name),
            Port(name=q_out, direction="output"),
        ],
        clocks=[src, dst],
        prediction=Prediction(
            cdc_rules_added=frozenset({"CDC-006"}),
            rationale="comb source feeds sync chain without source-side flop",
        ),
    )


def _emit_gray_counter_crossing(ctx: GenContext) -> Fragment:
    """Wide bus crossing with ``(* cdc_gray *)`` on both endpoints.

    CDC-004 (multi-bit unrelated-flops crossing) stays silent
    because the attribute promises gray encoding — every transition
    flips exactly one bit, so a metastable sample is one of the two
    legal adjacent codes, not a transient illegal mix.
    """
    src, dst = _pick_clocks(ctx)
    suffix = ctx.uniq("gray")
    en_in = f"en_in_{suffix}"
    q_out = f"q_out_{suffix}"
    bin_q = f"bin_q_{suffix}"
    gray_q = f"gray_q_{suffix}"
    sync_m = f"sync_m_{suffix}"
    sync_q = f"sync_q_{suffix}"

    decls = [
        f"    logic [3:0] {bin_q};",
        f"    (* cdc_gray *) logic [3:0] {gray_q};",
        f"    (* cdc_gray *) logic [3:0] {sync_m}, {sync_q};",
    ]
    always = [
        (
            f"    always_ff @(posedge {src.name}) begin\n"
            f"        if ({en_in}) {bin_q} <= {bin_q} + 4'd1;\n"
            f"        {gray_q} <= {bin_q} ^ ({bin_q} >> 1);\n"
            f"    end"
        ),
        (
            f"    always_ff @(posedge {dst.name}) begin\n"
            f"        {sync_m} <= {gray_q};\n"
            f"        {sync_q} <= {sync_m};\n"
            f"    end"
        ),
    ]
    assigns = [f"    assign {q_out} = {sync_q};"]
    return Fragment(
        decls=decls,
        always_blocks=always,
        assigns=assigns,
        ports=[
            Port(name=en_in, direction="input", sampling_clock=src.name),
            Port(name=q_out, direction="output", width=4),
        ],
        clocks=[src, dst],
        prediction=Prediction(
            rationale="(* cdc_gray *)-marked wide crossing; CDC-004 silenced",
        ),
    )


def _emit_missing_reset_sync(ctx: GenContext) -> Fragment:
    """Foreign-domain async reset on a dst-domain flop's ARST pin.

    Same failure mode as ``rdc001_async_reset_crossing`` in the
    hand-authored corpus: assertion is fine, but recovery/removal
    timing on deassertion is asynchronous to ``dst_clk``. RDC-001
    fires.
    """
    src, dst = _pick_clocks(ctx)
    suffix = ctx.uniq("rdc")
    global_rst_n = f"global_rst_n_{suffix}"
    rst_req = f"rst_req_{suffix}"
    d_in = f"d_in_{suffix}"
    q_out = f"q_out_{suffix}"
    local_rst_n = f"local_rst_n_{suffix}"
    dst_q = f"dst_q_{suffix}"

    decls = [
        f"    logic {local_rst_n};",
        f"    logic {dst_q};",
    ]
    always = [
        (
            f"    always_ff @(posedge {src.name} or negedge {global_rst_n})\n"
            f"        if (!{global_rst_n}) {local_rst_n} <= 1'b0;\n"
            f"        else                  {local_rst_n} <= ~{rst_req};"
        ),
        (
            f"    always_ff @(posedge {dst.name} or negedge {local_rst_n})\n"
            f"        if (!{local_rst_n}) {dst_q} <= 1'b0;\n"
            f"        else                {dst_q} <= {d_in};"
        ),
    ]
    assigns = [f"    assign {q_out} = {dst_q};"]
    return Fragment(
        decls=decls,
        always_blocks=always,
        assigns=assigns,
        ports=[
            Port(name=global_rst_n, direction="input"),
            Port(name=rst_req, direction="input", sampling_clock=src.name),
            Port(name=d_in, direction="input", sampling_clock=dst.name),
            Port(name=q_out, direction="output"),
        ],
        clocks=[src, dst],
        prediction=Prediction(
            cdc_rules_added=frozenset({"RDC-001"}),
            rationale="foreign-domain async reset directly on dst-domain ARST",
        ),
    )


PRODUCTIONS: tuple[Production, ...] = (
    Production(
        name="clean_sync_chain",
        emit=_emit_clean_sync_chain,
        declared=Prediction(rationale="clean 2FF synchroniser"),
    ),
    Production(
        name="unsynced_single_bit",
        emit=_emit_unsynced_single_bit,
        declared=Prediction(
            cdc_rules_added=frozenset({"CDC-001"}),
            rationale="unsynced single-bit crossing",
        ),
    ),
    Production(
        name="comb_source",
        emit=_emit_comb_source,
        declared=Prediction(
            cdc_rules_added=frozenset({"CDC-006"}),
            rationale="comb source feeds sync chain",
        ),
    ),
    Production(
        name="gray_counter_crossing",
        emit=_emit_gray_counter_crossing,
        declared=Prediction(rationale="(* cdc_gray *)-marked wide crossing"),
    ),
    Production(
        name="missing_reset_sync",
        emit=_emit_missing_reset_sync,
        declared=Prediction(
            cdc_rules_added=frozenset({"RDC-001"}),
            rationale="async reset crossing on dst-domain ARST",
        ),
    ),
)
