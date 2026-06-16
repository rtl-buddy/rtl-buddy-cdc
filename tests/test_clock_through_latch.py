"""P2 clock-path latch (issue #263) — a clock routed through a transparent
latch now resolves to its upstream clock root.

``domain._trace`` follows buffers, two-input clock gates, muxes and flop-Q
dividers. P2 adds the clock-path latch: when a flop's CLK net is driven by a
``$dlatch`` / ``$_DLATCH_*`` Q, the walk explores the latch's data pin (``D``)
and enable pin (``EN`` coarse / ``E`` gate-level), returning whichever leg
resolves to a clock root — mirroring the two-input gate clause.

``clock_through_latch`` exercises BOTH ICG coding styles:

  - D-leg (``dq``): ``always_latch if (en) gclk_d = clk_a;`` — the clock enters
    on the latch's ``D`` pin.
  - EN-leg (``eq``): a gated clock drives the latch ENABLE while a constant
    level holds on ``D`` — the clock enters on the ``EN`` pin.

Both resolve to ``clk_a`` where they were domain-unknown before P2. The fixture
also carries a genuine ``clk_a → clk_b`` crossing as a parity anchor.

PARITY: latch transparency applies ONLY to clock-root resolution. The
crossing/violation set must be identical whether the latch clause fires or not
— the change can only ADD a resolved domain, never drop, gain, or perturb a
crossing. ``test_parity_with_latch_clause_disabled`` proves this by disabling
the clause (monkeypatching ``is_latch_cell`` to ``False`` inside ``domain``,
restoring the pre-P2 tracer) and comparing the full crossing/violation set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import domain as domain_mod, netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "clock_through_latch"
JSON = FIX_DIR / "clock_through_latch.json"
SDC = FIX_DIR / "clock_through_latch.sdc"

# The simple latch-based ICG ($and with a direct-clk A-leg + a $dlatch on the
# enable) ALREADY resolved before P2 — pin that it still does (no regression).
ICG_DIR = Path(__file__).parent / "fixtures" / "clock_gating"
ICG_JSON = ICG_DIR / "clock_gating.json"
ICG_SDC = ICG_DIR / "clock_gating.sdc"


def _skip_if_missing(p: Path) -> None:
    if not p.exists():
        pytest.skip(f"fixture not built: {p}")


def _domains_by_q_netname(module: netlist.Module, spec: sdc_mod.ClockSpec) -> dict:
    """Map each flop's first Q netname → resolved clock, for readable asserts."""
    bit2name: dict[int, str] = {}
    for nn_name, nn in module.netnames.items():
        for b in nn.bits:
            if isinstance(b, int):
                bit2name.setdefault(b, nn_name)
    out: dict[str, str | None] = {}
    for fd in assign_domains(
        module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
    ):
        names = [bit2name.get(b) for b in fd.flop.q if isinstance(b, int)]
        if names and names[0] is not None:
            out[names[0]] = fd.clock
    return out


def test_clock_path_latch_flops_resolve_to_clk_a() -> None:
    """Both the D-leg (``dq``) and EN-leg (``eq``) latch-clocked flops resolve
    to ``clk_a`` — the clock is followed through the latch on whichever pin it
    enters."""
    _skip_if_missing(JSON)
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    by_q = _domains_by_q_netname(module, spec)
    assert by_q.get("dq") == "clk_a", f"D-leg latch flop did not resolve: {by_q}"
    assert by_q.get("eq") == "clk_a", f"EN-leg latch flop did not resolve: {by_q}"
    # The genuine clk_a/clk_b registers still resolve as before.
    assert by_q.get("a_q") == "clk_a"
    assert by_q.get("b_q") == "clk_b"
    # No flop is left domain-unknown.
    assert all(v is not None for v in by_q.values()), by_q


def test_simple_icg_still_resolves() -> None:
    """The simple latch-based ICG ($and direct-clk A-leg) resolved before P2
    and must still resolve — P2 must not perturb the already-handled shape."""
    _skip_if_missing(ICG_JSON)
    module = netlist.load(ICG_JSON)
    spec = sdc_mod.parse_file(ICG_SDC)
    domains = assign_domains(
        module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
    )
    # Every flop in the clock-gating fixture has a resolved domain.
    assert domains
    assert all(d.clock is not None for d in domains), [
        d.flop.cell.name for d in domains if d.clock is None
    ]


def _analyze(json_path: Path, sdc_path: Path) -> tuple[list, list]:
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    violations = run_all_rules(module, async_crossings, spec)
    return crossings, violations


def _cross_keys(crossings: list) -> list[tuple]:
    return sorted(
        (c.src_name, c.dst_flop.cell.name, c.src_clock, c.dst_clock, c.width)
        for c in crossings
    )


def _viol_keys(violations: list) -> list[tuple]:
    return sorted((v.rule_id, v.cell_name or "") for v in violations)


def test_genuine_crossing_present() -> None:
    """The fixture carries a real clk_a→clk_b async crossing (so the parity
    test below is not vacuous)."""
    _skip_if_missing(JSON)
    crossings, violations = _analyze(JSON, SDC)
    keys = _cross_keys(crossings)
    assert ("$procdff$39", "$procdff$38", "clk_a", "clk_b", 1) in keys
    # The unsynchronised crossing fires CDC-001.
    assert ("CDC-001", "$procdff$38") in _viol_keys(violations)


def test_parity_with_latch_clause_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """PARITY: disabling the P2 latch clause (restoring the pre-P2 tracer,
    which leaves the latch-clocked flops domain-unknown) yields the IDENTICAL
    crossing and violation set. The clause only adds resolved domains; it never
    drops, gains, or perturbs a crossing.

    Disabling is done by forcing ``domain.is_latch_cell`` to ``False`` — the
    exact predicate guarding the new clause — so the walk behaves as it did
    before P2 on the clock path while every other tracer category is untouched.
    """
    _skip_if_missing(JSON)
    with_clause_cross, with_clause_viol = _analyze(JSON, SDC)

    monkeypatch.setattr(domain_mod, "is_latch_cell", lambda _ct: False)
    plain_cross, plain_viol = _analyze(JSON, SDC)

    assert _cross_keys(with_clause_cross) == _cross_keys(plain_cross)
    assert _viol_keys(with_clause_viol) == _viol_keys(plain_viol)
