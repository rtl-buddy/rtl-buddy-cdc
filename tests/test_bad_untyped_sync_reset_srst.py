"""Untyped sync reset on an ``$sdff`` ``SRST`` pin — CDC-011 must fire.

Issue rtl-buddy-cdc#272. The crossing model is ``D``-pin-scoped:
:func:`find_crossings` seeds and terminates on flop ``D`` pins, so a
top-level input that lands on a dedicated ``SRST`` pin produces no
:class:`~rtl_buddy_cdc.domain.Crossing` record. CDC-011 consumes
port-sourced crossings, so an untyped synchronous reset used to vanish
silently — while the *same* reset, when the lowering folded it into a
``$dff`` D-cone mux, was reported.

``check_cdc_011`` now supplements the crossing-derived destinations
with a direct ``SRST``-pin walk (``_unconstrained_srst_captures``), so
the finding no longer depends on which flop cell the synthesis pass
inferred. The two fixtures here are the same RTL and the same SDC
built two ways — with ``opt_dff`` (``$sdff`` + ``SRST``) and without
(``$dff`` + ``$mux``-on-``D``) — and the parity test asserts the rule
id, severity and message text match exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import check_cdc_011, run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"
SRST_FIX = "bad_untyped_sync_reset_srst"
MUX_FIX = "bad_untyped_sync_reset_mux"


def _analyze(name: str):
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


@pytest.fixture(scope="module")
def srst_case():
    return _analyze(SRST_FIX)


@pytest.fixture(scope="module")
def mux_case():
    return _analyze(MUX_FIX)


def test_srst_arrival_produces_no_crossing(srst_case) -> None:
    """The root cause, pinned: ``srst`` reaches an ``SRST`` pin, and the
    D-pin-scoped crossing walk cannot see it. Only ``dctl`` (a genuine
    ``D``-pin capture) shows up as a port-sourced crossing."""
    _module, crossings, _spec = srst_case
    ports = sorted({c.src_port for c in crossings if c.src_port is not None})
    assert ports == ["dctl"], (
        f"expected the SRST arrival to be invisible to find_crossings "
        f"(D-pin-scoped); got port-sourced crossings for {ports}"
    )


def test_cdc_011_fires_on_the_untyped_sync_reset(srst_case) -> None:
    """CDC-011 reports the untyped reset despite the missing crossing."""
    module, crossings, spec = srst_case
    violations = run_all_rules(module, crossings, spec)
    cdc_011 = [v for v in violations if v.rule_id == "CDC-011"]
    ports = sorted(v.message.split("'")[1] for v in cdc_011)
    assert ports == ["dctl", "srst"], (
        f"expected CDC-011 on both untyped inputs; got {ports} from "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    srst_v = next(v for v in cdc_011 if "'srst'" in v.message)
    assert srst_v.severity == "warning", (
        f"single destination domain → warning; got {srst_v.severity}"
    )
    assert "set_input_delay -clock clk [get_ports srst]" in srst_v.message
    assert srst_v.cell_name is not None


def test_only_cdc_011_fires(srst_case) -> None:
    """No other rule may fire on this fixture — in particular RDC-003,
    which keys on a *flop*-sourced SRST and must stay silent on a
    port-sourced one."""
    module, crossings, spec = srst_case
    violations = run_all_rules(module, crossings, spec)
    assert {v.rule_id for v in violations} == {"CDC-011"}, [
        (v.rule_id, v.message) for v in violations
    ]


def test_lowering_parity_srst_vs_mux(srst_case, mux_case) -> None:
    """The load-bearing invariant of #272: the same RTL + SDC must
    produce the same findings whether the sync reset lowered onto an
    ``SRST`` pin or into a ``$dff`` D-cone mux.

    Cell names differ between the two builds, so the comparison is on
    (rule_id, severity, message) — everything the user sees.
    """

    def _fingerprint(case):
        module, crossings, spec = case
        return sorted(
            (v.rule_id, v.severity, v.message)
            for v in run_all_rules(module, crossings, spec)
        )

    srst_findings = _fingerprint(srst_case)
    mux_findings = _fingerprint(mux_case)
    assert srst_findings == mux_findings, (
        f"lowering-dependent findings (issue #272):\n"
        f"  $sdff/SRST: {srst_findings}\n"
        f"  $dff/mux  : {mux_findings}"
    )
    # Sanity: the parity is not vacuous.
    assert len(srst_findings) == 2


def test_mux_variant_sees_both_ports_as_crossings(mux_case) -> None:
    """Counterpart to :func:`test_srst_arrival_produces_no_crossing` —
    with the mux lowering both untyped ports land on ``D`` pins, so the
    ordinary port walk emits a crossing for each."""
    _module, crossings, _spec = mux_case
    ports = sorted({c.src_port for c in crossings if c.src_port is not None})
    assert ports == ["dctl", "srst"]


def test_standalone_call_builds_its_own_context(srst_case) -> None:
    """``check_cdc_011`` is reachable outside ``run_all``; without a
    pre-built ``ctx`` it must lazy-build one and still walk the SRST
    pins."""
    module, crossings, spec = srst_case
    violations = check_cdc_011(module, crossings, spec)
    assert sorted(v.message.split("'")[1] for v in violations) == ["dctl", "srst"]


def test_no_sdc_skips_the_srst_walk(srst_case) -> None:
    """Without an SDC there is no "untyped" verdict to make, so only the
    crossing-derived destinations remain. The fixture's crossings were
    built with the sentinel, so ``dctl`` still reports; ``srst`` — which
    exists only via the SRST walk — does not."""
    module, crossings, _spec = srst_case
    violations = check_cdc_011(module, crossings, None)
    assert sorted(v.message.split("'")[1] for v in violations) == ["dctl"]
