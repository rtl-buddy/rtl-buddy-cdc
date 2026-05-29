"""Coverage-focused tests for the CDC-001..CDC-010 rule pack and its
shared structural helpers in :mod:`rtl_buddy_cdc.rules`.

These tests exercise paths the committed paired fixtures don't reach on
their own:

* the **lazy-context** branch in every ``check_cdc_NNN`` (calling a rule
  directly, with ``ctx=None``, so the rule builds its own
  ``_RuleContext`` / ``reader_counts`` / ``d_bit_to_single_bit_flop``);
* the early-return guards in ``_chain_has_inter_stage_comb`` and
  ``_is_multibit_sync_first_stage``;
* CDC-008's gate-level-FF ``C``-pin exemption and per-(bit,cell,pin)
  dedupe;
* CDC-010's empty-control-pin skip, the ``clock_spec is None``
  ``_async`` fallback, and the ``(* glitchless_clock_mux *)``
  suppression;
* the ``"high"`` polarity branch and the reset-hints overlay in
  ``user_reset_polarity_overrides``.

Everything is built either from a committed Yosys-JSON fixture
(``netlist.load`` — no toolchain) or from hand-constructed
``Module`` / ``Cell`` / ``Netname`` dataclasses, mirroring the
self-contained style of ``tests/test_rule_corners.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import (
    _build_context,  # noqa: PLC2701
    _chain_has_inter_stage_comb,  # noqa: PLC2701
    _is_multibit_sync_first_stage,  # noqa: PLC2701
    _sync_chain_depth,  # noqa: PLC2701
    check_cdc_001,
    check_cdc_002,
    check_cdc_003,
    check_cdc_004,
    check_cdc_005,
    check_cdc_006,
    check_cdc_008,
    check_cdc_010,
    run_all,
    user_reset_polarity_overrides,
)
from rtl_buddy_cdc.sdc import parse as parse_sdc

FIX_ROOT = Path(__file__).parent / "fixtures"

PYYAML_INSTALLED = importlib.util.find_spec("yaml") is not None


# --- shared helpers ---------------------------------------------------------


def _load_fixture(name: str) -> tuple[Module, sdc_mod.ClockSpec]:
    """Load a committed fixture's netlist + SDC. Skips if not built."""
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    return module, spec


def _async_crossings(module: Module, spec: sdc_mod.ClockSpec) -> list:
    """The async-filtered crossing list every fixture test re-derives."""
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]


# --- lazy-context branch in each rule ---------------------------------------
#
# Every ``check_cdc_NNN`` lazily builds its own ``_RuleContext`` when
# called with ``ctx=None``. The fixture-driven tests always go through
# ``run_all`` (which pre-builds the context), so the lazy branch — and,
# transitively, ``_bit_reader_count`` and the lazy
# ``d_bit_to_single_bit_flop`` build inside ``_sync_chain_depth`` — is
# only reached on a direct call. We use ``bad_single_ff_sync`` because
# it has a real single-bit flop→flop crossing that CDC-001 fires on.


def test_check_cdc_001_lazy_context_fires() -> None:
    """Calling ``check_cdc_001`` directly (``ctx=None``) builds its own
    context and still fires on a single-flop crossing — exercising the
    lazy ``_build_context`` / ``_bit_reader_count`` paths."""
    module, spec = _load_fixture("bad_single_ff_sync")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_001(module, crossings, spec)  # no ctx=
    assert len(violations) == 1
    v = violations[0]
    assert v.rule_id == "CDC-001"
    assert v.severity == "error"
    assert "unsynchronized control crossing" in v.message
    assert "chain depth = 1" in v.message


def test_check_cdc_002_lazy_context_fires_when_depth_raised() -> None:
    """``check_cdc_002`` with ``required_depth=3`` and ``ctx=None`` must
    build its own context and fire on a 2FF chain that's below the
    raised bar."""
    module, spec = _load_fixture("good_2ff_sync")
    crossings = _async_crossings(module, spec)
    silent = check_cdc_002(module, crossings, spec, 2)  # default bar
    assert silent == []
    raised = check_cdc_002(module, crossings, spec, 3)  # no ctx=
    assert len(raised) == 1
    assert raised[0].rule_id == "CDC-002"
    assert raised[0].severity == "warning"
    assert "required >= 3" in raised[0].message


def test_check_cdc_002_skips_user_marked_synchronizer() -> None:
    """At a raised ``required_depth`` of 3, a crossing whose destination
    flop is a ``(* cdc_sync *)``-marked synchronizer is skipped (the
    ``c.dst_flop ... in ctx.user_syncs`` branch, rules.py line 888).
    The user vouches for the synchronizer shape regardless of measured
    depth, so CDC-002 stays silent."""
    module, spec = _load_fixture("marked_single_ff_sync")
    crossings = _async_crossings(module, spec)
    assert crossings, "fixture should expose the marked crossing"
    violations = check_cdc_002(module, crossings, spec, 3)
    assert [v for v in violations if v.rule_id == "CDC-002"] == []


def test_check_cdc_002_skips_quasi_static_source() -> None:
    """A 1-bit crossing whose *source* flop is ``(* cdc_static *)`` is
    skipped at the static-source branch (rules.py line 890): the value
    is held constant during operation, so cross-domain sampling is
    coherent and synchronizer depth is moot."""
    module, spec = _load_fixture("marked_quasi_static")
    crossings = _async_crossings(module, spec)
    one_bit = [c for c in crossings if c.width == 1]
    assert one_bit, "fixture should expose a 1-bit quasi-static crossing"
    violations = check_cdc_002(module, crossings, spec, 3)
    assert [v for v in violations if v.rule_id == "CDC-002"] == []


def test_check_cdc_002_skips_unconstrained_port_crossing() -> None:
    """CDC-002's unconstrained-port skip (rules.py line 886): a 1-bit
    crossing whose ``src_clock`` is the unconstrained sentinel belongs
    to CDC-011, not CDC-002. Even with the depth bar raised to 3,
    CDC-002 must stay silent on those crossings."""
    module, spec = _load_fixture("bad_unconstrained_input_two_domains")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    unconstrained = [
        c for c in crossings if c.src_clock == sdc_mod.UNCONSTRAINED_SENTINEL
    ]
    assert unconstrained, "fixture should expose unconstrained-port crossings"
    violations = check_cdc_002(module, crossings, spec, 3)
    assert [v for v in violations if v.rule_id == "CDC-002"] == []


def test_check_cdc_001_skips_quasi_static_source() -> None:
    """CDC-001's static-source branch (rules.py line 824): the marked
    1-bit quasi-static crossing must not be reported as an
    unsynchronized control crossing."""
    module, spec = _load_fixture("marked_quasi_static")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_001(module, crossings, spec)
    assert [v for v in violations if v.rule_id == "CDC-001"] == []


def test_check_cdc_003_lazy_context() -> None:
    """``check_cdc_003`` called directly fires on the comb-between-source
    -and-synchronizer shape, building its own context."""
    module, spec = _load_fixture("bad_comb_before_sync")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_003(module, crossings, spec)  # no ctx=
    cdc_003 = [v for v in violations if v.rule_id == "CDC-003"]
    assert len(cdc_003) >= 1
    assert cdc_003[0].severity == "error"
    assert "combinational logic between source flop" in cdc_003[0].message


def test_check_cdc_005_lazy_context() -> None:
    """``check_cdc_005`` called directly fires on the reconvergent-sync
    fixture, building its own context (covers the ``ctx is None``
    branch and the grouped-walk code path)."""
    module, spec = _load_fixture("bad_reconvergent_with_recombine")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_005(module, crossings, spec)  # no ctx=
    cdc_005 = [v for v in violations if v.rule_id == "CDC-005"]
    assert len(cdc_005) >= 1
    assert cdc_005[0].severity == "warning"
    assert "reconvergent synchronizers" in cdc_005[0].message


def test_check_cdc_006_lazy_context() -> None:
    """``check_cdc_006`` called directly fires on the glitchy-comb-source
    fixture, building its own context."""
    module, spec = _load_fixture("bad_comb_source")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_006(module, crossings, spec)  # no ctx=
    cdc_006 = [v for v in violations if v.rule_id == "CDC-006"]
    assert len(cdc_006) == 1
    assert cdc_006[0].severity == "error"
    assert "glitchy combinational source" in cdc_006[0].message


def test_check_cdc_004_lazy_context_fires_on_binary_fifo_ptrs() -> None:
    """The async-FIFO with *binary* pointers crosses two multi-bit buses
    with no gating and a non-gray source — CDC-004 must fire on both.

    Calling ``check_cdc_004`` directly exercises the lazy-context
    branch; the binary pointers exercise the
    ``_is_multibit_sync_first_stage`` ∧ ``_is_gray_encoded_source``
    composite that *fails* the gray-coded-crossing exemption (the
    source is binary, so ``_is_gray_encoded_source`` returns False)."""
    module, spec = _load_fixture("bad_async_fifo_binary_ptrs")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_004(module, crossings, spec)  # no ctx=
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) == 2, (
        f"expected both binary pointer crossings to fire CDC-004, got "
        f"{[v.message for v in cdc_004]}"
    )
    for v in cdc_004:
        assert v.severity == "error"
        assert "unprotected bus crossing" in v.message


# --- _sync_chain_depth lazy build (reader_counts / d_bit map None) ----------


def test_sync_chain_depth_lazy_builds_internal_indexes() -> None:
    """``_sync_chain_depth`` called with both ``reader_counts=None`` and
    ``d_bit_to_single_bit_flop=None`` rebuilds them internally. On a
    2FF chain the head flop reports depth 2."""
    module, spec = _load_fixture("good_2ff_sync")
    crossings = _async_crossings(module, spec)
    assert crossings, "fixture should have at least one crossing"
    c = crossings[0]
    ctx = _build_context(module, spec)
    # Call with the lazy-build path: omit reader_counts and the d-bit map.
    depth = _sync_chain_depth(module, c.dst_flop, c.dst_clock, ctx.domains)
    assert depth == 2, f"2FF synchronizer head should be depth 2, got {depth}"


# --- _chain_has_inter_stage_comb guard branches -----------------------------
#
# The CDC-014-firing fixture exercises the *match* path; here we pin the
# early-return guards with hand-built flops so a refactor can't silently
# turn one of them into a false match.


def _single_flop(name: str, clk: int, d: tuple[int, ...], q: tuple[int, ...]) -> Flop:
    cell = Cell(
        name=name,
        type="$dff",
        connections={"CLK": (clk,), "D": d, "Q": q},
    )
    return Flop(cell=cell, clk=clk, d=d, q=q)


def test_chain_has_inter_stage_comb_returns_none_for_multibit_head() -> None:
    """A width-2 head can't be a single-bit sync-chain stage; the helper
    bails immediately (``len(head.q) != 1``)."""
    head = _single_flop("head", clk=2, d=(3, 4), q=(5, 6))
    module = Module(name="m", ports={}, cells={"head": head.cell}, netnames={})
    ctx = _build_context(module, clock_spec=None)
    assert _chain_has_inter_stage_comb(head, ctx) is None


def test_chain_has_inter_stage_comb_returns_none_for_constant_q() -> None:
    """A head whose single Q bit is a constant (string) can't drive a
    follow-on stage; the ``not isinstance(head_q, int)`` guard fires."""
    head = _single_flop("head", clk=2, d=(3,), q=("0",))
    module = Module(name="m", ports={}, cells={"head": head.cell}, netnames={})
    ctx = _build_context(module, clock_spec=None)
    assert _chain_has_inter_stage_comb(head, ctx) is None


def test_chain_has_inter_stage_comb_matches_gate_then_flop() -> None:
    """Positive path on a hand-built module: head.Q feeds an ``$and``
    whose Y drives a same-domain flop's D. The helper returns that
    downstream flop — the structural shape CDC-014 reports and CDC-001
    defers on."""
    clk = 2
    head = Cell(
        name="head", type="$dff", connections={"CLK": (clk,), "D": (3,), "Q": (4,)}
    )
    gate = Cell(name="gate", type="$and", connections={"A": (4,), "B": (9,), "Y": (5,)})
    follow = Cell(
        name="follow", type="$dff", connections={"CLK": (clk,), "D": (5,), "Q": (6,)}
    )
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={"head": head, "gate": gate, "follow": follow},
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    matched = _chain_has_inter_stage_comb(head_flop, ctx)
    assert matched is not None
    assert matched.cell.name == "follow"


# --- _is_multibit_sync_first_stage guard branches ---------------------------


def test_chain_has_inter_stage_comb_rejects_foreign_domain_followon() -> None:
    """When the comb cell's output drives a flop in a *different* clock
    domain, the inter-stage-comb pattern does not match — the follow-on
    isn't a same-domain sync stage (the domain-mismatch ``continue``,
    rules.py line 786)."""
    clk_a, clk_b = 2, 3
    head = Cell(
        name="head", type="$dff", connections={"CLK": (clk_a,), "D": (4,), "Q": (5,)}
    )
    gate = Cell(name="gate", type="$or", connections={"A": (5,), "B": (10,), "Y": (6,)})
    # Follow-on flop sits in the clk_b domain, not head's clk_a domain.
    foreign = Cell(
        name="foreign",
        type="$dff",
        connections={"CLK": (clk_b,), "D": (6,), "Q": (7,)},
    )
    module = Module(
        name="m",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells={"head": head, "gate": gate, "foreign": foreign},
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    assert _chain_has_inter_stage_comb(head_flop, ctx) is None


def test_chain_has_inter_stage_comb_skips_direct_flop_and_dead_comb() -> None:
    """Exercise the per-consumer guards in ``_chain_has_inter_stage_comb``:

    * a direct flop consumer of ``head.Q`` is skipped (it's a plain
      chain extension, not the inter-stage-comb shape — rules.py line
      775);
    * a comb cell whose ``Y`` drives a net with no single-bit flop
      consumer yields no match (line 782);
    * a comb cell whose ``Y`` feeds back to ``head`` itself is ignored
      (line 784).

    None of these constitute the gate-then-stage shape, so the helper
    returns ``None``.
    """
    clk = 2
    head_q = 4
    dead_y = 6
    fb_y = 3  # feedback gate output wired back into head's own D
    head = Cell(
        name="head",
        type="$dff",
        connections={"CLK": (clk,), "D": (fb_y,), "Q": (head_q,)},
    )
    # Direct flop consumer of head.Q (line 775 continue).
    direct = Cell(
        name="direct",
        type="$dff",
        connections={"CLK": (clk,), "D": (head_q,), "Q": (9,)},
    )
    # Comb cell whose Y (dead_y) has no flop consumer (line 782).
    dead_gate = Cell(
        name="dead_gate",
        type="$not",
        connections={"A": (head_q,), "Y": (dead_y,)},
    )
    # Comb cell whose Y feeds back into head's own D (line 784).
    fb_gate = Cell(
        name="fb_gate",
        type="$not",
        connections={"A": (head_q,), "Y": (fb_y,)},
    )
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "head": head,
            "direct": direct,
            "dead_gate": dead_gate,
            "fb_gate": fb_gate,
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    assert _chain_has_inter_stage_comb(head_flop, ctx) is None


def test_is_multibit_sync_first_stage_rejects_single_bit() -> None:
    """A 1-bit flop is never a *multi-bit* sync first stage (the
    ``len(dst_flop.q) < 2`` guard)."""
    flop = _single_flop("f", clk=2, d=(3,), q=(4,))
    module = Module(name="m", ports={}, cells={"f": flop.cell}, netnames={})
    assert not _is_multibit_sync_first_stage(module, flop, "clk", {"f": "clk"})


def test_is_multibit_sync_first_stage_rejects_mismatched_d_width() -> None:
    """A flop whose D width differs from its Q width can't be a clean
    lane-aligned sync stage (the ``len(dst_flop.d) != width`` guard)."""
    flop = _single_flop("f", clk=2, d=(3,), q=(4, 5))
    module = Module(name="m", ports={}, cells={"f": flop.cell}, netnames={})
    assert not _is_multibit_sync_first_stage(module, flop, "clk", {"f": "clk"})


def test_is_multibit_sync_first_stage_rejects_constant_q_bit() -> None:
    """A width-2 flop one of whose Q bits is a constant string can't be a
    lane-aligned sync stage (the ``not all(isinstance(b, int) ...)``
    guard, rules.py line 1216)."""
    flop = _single_flop("f", clk=2, d=(3, 4), q=(5, "0"))
    module = Module(name="m", ports={}, cells={"f": flop.cell}, netnames={})
    assert not _is_multibit_sync_first_stage(module, flop, "clk", {"f": "clk"})


def test_is_multibit_sync_first_stage_accepts_lane_aligned_pair() -> None:
    """Two width-2 flops in the same domain wired lane-for-lane
    (stage0.Q == stage1.D) are recognised as a multi-bit sync chain."""
    clk = 2
    stage0 = Cell(
        name="stage0",
        type="$dff",
        connections={"CLK": (clk,), "D": (3, 4), "Q": (5, 6)},
    )
    stage1 = Cell(
        name="stage1",
        type="$dff",
        connections={"CLK": (clk,), "D": (5, 6), "Q": (7, 8)},
    )
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={"stage0": stage0, "stage1": stage1},
        netnames={},
    )
    stage0_flop = next(f for f in find_flops(module) if f.cell.name == "stage0")
    domains = {"stage0": "clk", "stage1": "clk"}
    assert _is_multibit_sync_first_stage(module, stage0_flop, "clk", domains)


# --- CDC-008: gate-level-FF C-pin exemption + dedupe + no-clock-bits ---------


def _gate_level_clock_as_data_module() -> Module:
    """A gate-level ``$_DFF_P_`` flop (clock on pin ``C``) whose clock net
    is ALSO wired into a comb cell's data pin AND into a second flop's
    ``C`` pin.

    Layout (clk net = bit 2)::

        clk ── leak.A   (data use → CDC-008 must fire here)
        clk ── ff_a.C   (clock use on gate-level flop → exempt, line 1641)
        clk ── ff_b.C   (second clock-net flop → C-pin exempt too)

    The single clock net read on two different ``C`` pins also drives
    the per-(bit,cell,pin) dedupe in the rule body.
    """
    clk = 2
    a_d = 3
    a_q = 4
    b_d = 5
    b_q = 6
    leak_y = 7
    ports = {
        "clk": Port(name="clk", direction="input", bits=(clk,)),
        "a_d": Port(name="a_d", direction="input", bits=(a_d,)),
        "b_d": Port(name="b_d", direction="input", bits=(b_d,)),
        "leak_q": Port(name="leak_q", direction="output", bits=(leak_y,)),
    }
    cells = {
        "ff_a": Cell(
            name="ff_a",
            type="$_DFF_P_",
            connections={"C": (clk,), "D": (a_d,), "Q": (a_q,)},
        ),
        "ff_b": Cell(
            name="ff_b",
            type="$_DFF_P_",
            connections={"C": (clk,), "D": (b_d,), "Q": (b_q,)},
        ),
        # Reads the clock net on a data pin — the genuine CDC-008 hazard.
        "leak": Cell(
            name="leak",
            type="$or",
            connections={"A": (clk,), "B": (a_q,), "Y": (leak_y,)},
        ),
    }
    return Module(name="gate_clk_as_data", ports=ports, cells=cells, netnames={})


def test_cdc_008_exempts_gate_level_ff_c_pin() -> None:
    """The clock net feeds two gate-level flops on their ``C`` pin and one
    comb cell on a data pin. CDC-008 must fire exactly once — on the
    comb data read — and must NOT false-fire on either ``C`` pin
    (the gate-level-FF exemption, rules.py line 1641)."""
    module = _gate_level_clock_as_data_module()
    spec = parse_sdc("create_clock -name clk -period 10.0 [get_ports clk]\n")
    violations = check_cdc_008(module, [], spec)  # no ctx= → lazy build
    cdc_008 = [v for v in violations if v.rule_id == "CDC-008"]
    assert len(cdc_008) == 1, (
        f"expected exactly one CDC-008 (the comb data read); got "
        f"{[v.message for v in cdc_008]}"
    )
    v = cdc_008[0]
    assert v.cell_name == "leak"
    assert "used as data" in v.message
    assert "$or" in v.message


def test_cdc_008_dedupes_repeated_clock_bit_on_same_pin() -> None:
    """A comb cell whose multi-bit ``A`` pin reads the same clock net on
    two lane indices yields a single CDC-008 finding, not two — the
    per-(bit, cell, pin) dedupe (rules.py line 1647)."""
    clk = 2
    ff_q = 3
    y0, y1 = 4, 5
    ports = {
        "clk": Port(name="clk", direction="input", bits=(clk,)),
        "y": Port(name="y", direction="output", bits=(y0, y1)),
    }
    cells = {
        "ff": Cell(
            name="ff",
            type="$dff",
            connections={"CLK": (clk,), "D": (ff_q,), "Q": (ff_q,)},
        ),
        # The clock net (bit 2) appears on BOTH lanes of the A pin.
        "leak": Cell(
            name="leak",
            type="$xor",
            connections={"A": (clk, clk), "B": (ff_q, ff_q), "Y": (y0, y1)},
        ),
    }
    module = Module(name="dup_clk", ports=ports, cells=cells, netnames={})
    spec = parse_sdc("create_clock -name clk -period 10.0 [get_ports clk]\n")
    violations = check_cdc_008(module, [], spec)
    cdc_008 = [v for v in violations if v.rule_id == "CDC-008"]
    assert len(cdc_008) == 1, (
        f"the duplicate clock-bit read on one pin must dedupe to a "
        f"single finding; got {[v.message for v in cdc_008]}"
    )
    assert cdc_008[0].cell_name == "leak"


def test_cdc_008_tolerates_sdc_clock_port_absent_from_module() -> None:
    """An SDC ``create_clock`` that names a port not present on the
    module is skipped when building the clock-bit set (rules.py line
    1606) — the analyzer doesn't crash on a stale SDC. A real flop
    clock still seeds the set, and the comb read of it still fires."""
    clk = 2
    ff_q = 3
    leak_y = 4
    ports = {
        "clk": Port(name="clk", direction="input", bits=(clk,)),
        "leak_q": Port(name="leak_q", direction="output", bits=(leak_y,)),
    }
    cells = {
        "ff": Cell(
            name="ff",
            type="$dff",
            connections={"CLK": (clk,), "D": (ff_q,), "Q": (ff_q,)},
        ),
        "leak": Cell(
            name="leak",
            type="$or",
            connections={"A": (clk,), "B": (ff_q,), "Y": (leak_y,)},
        ),
    }
    module = Module(name="stale_sdc", ports=ports, cells=cells, netnames={})
    # ``ghost_clk`` is declared on a port the module doesn't have.
    spec = parse_sdc(
        "create_clock -name clk -period 10.0 [get_ports clk]\n"
        "create_clock -name ghost_clk -period 5.0 [get_ports ghost_clk]\n"
    )
    violations = check_cdc_008(module, [], spec)
    cdc_008 = [v for v in violations if v.rule_id == "CDC-008"]
    assert len(cdc_008) == 1
    assert cdc_008[0].cell_name == "leak"


def test_cdc_008_silent_when_no_clock_bits() -> None:
    """With no flops and no SDC clocks, the clock-bit set is empty and
    CDC-008 returns early (rules.py line 1611)."""
    module = Module(
        name="empty",
        ports={"a": Port(name="a", direction="input", bits=(2,))},
        cells={
            "g": Cell(name="g", type="$not", connections={"A": (2,), "Y": (3,)}),
        },
        netnames={},
    )
    assert check_cdc_008(module, [], clock_spec=None) == []


# --- CDC-010: lazy ctx, clock_spec-None _async, empty control pin -----------


def test_cdc_010_lazy_context_no_sdc_async_fallback() -> None:
    """``check_cdc_010`` with ``ctx=None`` and ``clock_spec=None`` builds
    its own context and uses the ``a != b`` async fallback (rules.py
    line 1890).

    The gated clocks are produced by two divider flops on distinct
    clock-net domains (``ck_a`` / ``ck_b``) so ``_clock_input_domains_for``
    can classify them without an SDC; the mux select comes from a third
    flop on ``ck_sel``. Without an SDC every distinct domain is async,
    so CDC-010 fires::

        ck_a ── div_a.CLK ; div_a.Q ── mux.B ─┐
        ck_b ── div_b.CLK ; div_b.Q ── mux.A ─┤ ($mux Y = ck_out) ── out_ff.CLK
                                              │ S = sel_ff.Q  (clocked by ck_sel)
    """
    ck_a, ck_b, ck_sel = 2, 3, 4
    div_a_q, div_b_q = 5, 6
    sel_d, sel_q = 7, 8
    ck_out, d_in, q_out = 9, 10, 11
    ports = {
        "ck_a": Port(name="ck_a", direction="input", bits=(ck_a,)),
        "ck_b": Port(name="ck_b", direction="input", bits=(ck_b,)),
        "ck_sel": Port(name="ck_sel", direction="input", bits=(ck_sel,)),
        "sel_d": Port(name="sel_d", direction="input", bits=(sel_d,)),
        "d_in": Port(name="d_in", direction="input", bits=(d_in,)),
        "q_out": Port(name="q_out", direction="output", bits=(q_out,)),
    }
    cells = {
        # Divider flops give the mux's gated clocks a classifiable
        # (flop-domain) source even without an SDC.
        "div_a": Cell(
            name="div_a",
            type="$dff",
            connections={"CLK": (ck_a,), "D": (div_a_q,), "Q": (div_a_q,)},
        ),
        "div_b": Cell(
            name="div_b",
            type="$dff",
            connections={"CLK": (ck_b,), "D": (div_b_q,), "Q": (div_b_q,)},
        ),
        "sel_ff": Cell(
            name="sel_ff",
            type="$dff",
            connections={"CLK": (ck_sel,), "D": (sel_d,), "Q": (sel_q,)},
        ),
        "clk_mux": Cell(
            name="clk_mux",
            type="$mux",
            connections={
                "A": (div_b_q,),
                "B": (div_a_q,),
                "S": (sel_q,),
                "Y": (ck_out,),
            },
        ),
        "out_ff": Cell(
            name="out_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (d_in,), "Q": (q_out,)},
        ),
    }
    module = Module(name="async_clk_mux", ports=ports, cells=cells, netnames={})
    violations = check_cdc_010(module, [], clock_spec=None)  # lazy ctx + a!=b
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    assert len(cdc_010) == 1
    v = cdc_010[0]
    assert v.cell_name == "clk_mux"
    assert v.severity == "error"
    assert "control pin S" in v.message
    assert "sel_ff" in v.message


def test_cdc_010_skips_empty_control_pin() -> None:
    """A clock-network ``$mux`` whose ``S`` pin is present but carries no
    bits is skipped at the ``if not ctrl_bits`` guard (rules.py line
    1907) — no violation, no crash."""
    ck_a, ck_b = 2, 3
    ck_out, d_in, q_out = 7, 8, 9
    ports = {
        "ck_a": Port(name="ck_a", direction="input", bits=(ck_a,)),
        "ck_b": Port(name="ck_b", direction="input", bits=(ck_b,)),
        "d_in": Port(name="d_in", direction="input", bits=(d_in,)),
        "q_out": Port(name="q_out", direction="output", bits=(q_out,)),
    }
    cells = {
        "clk_mux": Cell(
            name="clk_mux",
            type="$mux",
            connections={"A": (ck_b,), "B": (ck_a,), "S": (), "Y": (ck_out,)},
        ),
        "out_ff": Cell(
            name="out_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (d_in,), "Q": (q_out,)},
        ),
    }
    module = Module(name="empty_sel_mux", ports=ports, cells=cells, netnames={})
    assert check_cdc_010(module, [], clock_spec=None) == []


def test_cdc_010_suppressed_by_glitchless_clock_mux_attr() -> None:
    """The ``good_glitchless_mux_marked`` fixture has its clock-mux select
    wire tagged ``(* glitchless_clock_mux *)``. CDC-010 must walk the
    select bit, recognise it as a user-vouched glitchless mux, and stay
    silent (rules.py lines 1908-1916)."""
    module, spec = _load_fixture("good_glitchless_mux_marked")
    ctx = _build_context(module, spec)
    # The select wire's bit is in the glitchless set — sanity-pins the
    # suppression precondition.
    assert ctx.user_glitchless_mux_bits, "fixture should mark a glitchless select"
    violations = check_cdc_010(module, [], spec, ctx=ctx)
    assert [v for v in violations if v.rule_id == "CDC-010"] == []


def test_cdc_010_fires_without_the_glitchless_attr() -> None:
    """Companion negative: the same clock-mux topology without the
    attribute (``bad_async_clock_mux``) DOES fire CDC-010 — proving the
    suppression above is the attribute's doing, not a structural
    accident."""
    module, spec = _load_fixture("bad_async_clock_mux")
    crossings = _async_crossings(module, spec)
    violations = run_all(module, crossings, spec)
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    assert len(cdc_010) >= 1
    assert cdc_010[0].severity == "error"


# --- user_reset_polarity_overrides: "high" branch + hints overlay -----------


def test_reset_polarity_override_high_value() -> None:
    """A top-level input port whose netname carries
    ``(* reset_polarity = "high" *)`` (any case / surrounding
    whitespace) decodes to ``"high"`` — the branch the committed
    ``"low"`` fixtures never reach (rules.py lines 419-420)."""
    module = Module(
        name="m",
        ports={"rst": Port(name="rst", direction="input", bits=(2,))},
        cells={},
        netnames={
            "rst": Netname(
                name="rst",
                bits=(2,),
                attributes={"reset_polarity": "  HIGH  "},
            ),
        },
    )
    assert user_reset_polarity_overrides(module) == {"rst": "high"}


def test_reset_polarity_override_unparseable_value_ignored() -> None:
    """A value that decodes to neither ``high`` nor ``low`` (e.g. a typo)
    is silently dropped — tolerant-input posture."""
    module = Module(
        name="m",
        ports={"rst": Port(name="rst", direction="input", bits=(2,))},
        cells={},
        netnames={
            "rst": Netname(name="rst", bits=(2,), attributes={"reset_polarity": "lo"}),
        },
    )
    assert user_reset_polarity_overrides(module) == {}


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_reset_polarity_override_hints_overlay_wins() -> None:
    """A reset-hints YAML port declaration overlays the SV-attribute map
    for ports that exist on the module (rules.py lines 424-427). The
    ``bad_hints_reset_polarity`` fixture ships an external ``low``
    declaration for ``rst_n`` and an attribute-free SV file, so the
    override map is produced *only* via the hints path."""
    from rtl_buddy_cdc.reset_hints import load as load_hints

    fix_dir = FIX_ROOT / "bad_hints_reset_polarity"
    json_path = fix_dir / "bad_hints_reset_polarity.json"
    hints_path = fix_dir / "bad_hints_reset_polarity.hints.yaml"
    if not json_path.exists() or not hints_path.exists():
        pytest.skip("fixture not built")
    module = netlist.load(json_path)
    hints = load_hints(hints_path)
    # No SV attribute on the port — the map is empty without hints.
    assert user_reset_polarity_overrides(module) == {}
    # With hints, the external declaration overlays in.
    overlaid = user_reset_polarity_overrides(module, hints=hints)
    assert overlaid.get("rst_n") == "low"


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_reset_polarity_hint_for_unknown_port_ignored() -> None:
    """A hint naming a port that doesn't exist on the module is dropped
    by the ``if name in port_names`` guard — no spurious entry."""
    from rtl_buddy_cdc.reset_hints import PortHint, ResetHints

    module = Module(
        name="m",
        ports={"rst_n": Port(name="rst_n", direction="input", bits=(2,))},
        cells={},
        netnames={},
    )
    hints = ResetHints(
        schema_version="1.0",
        ports=(PortHint(name="nonexistent", polarity="high", type="async"),),
        synchronizers=(),
    )
    assert user_reset_polarity_overrides(module, hints=hints) == {}
