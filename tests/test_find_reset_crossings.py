"""Unified ``find_reset_crossings`` API (issue #107).

The RDC rule family detects per-flop reset issues across five rules
(RDC-001..-005). External tooling (e.g. ``rtl-buddy-view``) wants the
same structural facts without re-implementing each rule's walk. This
test exercises ``find_reset_crossings`` against the existing RDC
fixtures to confirm it surfaces the same crossings the rule pack
would, classified by kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains
from rtl_buddy_cdc.reset_domain import find_reset_crossings, find_reset_synchronizers

FIX_ROOT = Path(__file__).parent / "fixtures"


def _load(fixture: str):
    name = fixture
    dir_ = FIX_ROOT / fixture
    json_path = dir_ / f"{name}.json"
    sdc_path = dir_ / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    flop_domains = assign_domains(module)
    clock_domains = {fd.flop.cell.name: fd.clock for fd in flop_domains}
    return module, clock_domains, spec


def test_bad_reset_crossing_emits_async_deassert() -> None:
    """``bad_reset_crossing`` is RDC-001's canonical bad case: an
    async reset crosses clock domains without a synchroniser.
    ``find_reset_crossings`` must emit at least one
    ``"async-deassert"`` for the consumer flop in the foreign
    domain."""
    module, clock_domains, _ = _load("bad_reset_crossing")
    syncs = find_reset_synchronizers(module, clock_domains)
    crossings = find_reset_crossings(module, clock_domains, recognised_syncs=syncs)
    kinds = {c.kind for c in crossings}
    assert "async-deassert" in kinds, (
        f"expected an async-deassert crossing, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_good_reset_sync_no_crossings() -> None:
    """``good_reset_sync`` has a proper reset-synchroniser chain.
    The recogniser's output, fed in as ``recognised_syncs``, must
    cause ``find_reset_crossings`` to skip every flop in the chain
    and return no crossings."""
    module, clock_domains, _ = _load("good_reset_sync")
    syncs = find_reset_synchronizers(module, clock_domains)
    crossings = find_reset_crossings(module, clock_domains, recognised_syncs=syncs)
    assert crossings == [], (
        f"expected no crossings on the good reset-sync fixture, got: "
        f"{[(c.flop, c.kind) for c in crossings]}"
    )


def test_bad_rdc_004_emits_comb_driven() -> None:
    """The RDC-004 bad fixture wires a reset through combinational
    logic. ``find_reset_crossings`` must classify that as
    ``"comb-driven"``."""
    module, clock_domains, _ = _load("bad_rdc_004_comb_driven_reset")
    crossings = find_reset_crossings(module, clock_domains)
    kinds = {c.kind for c in crossings}
    assert "comb-driven" in kinds, (
        f"expected a comb-driven crossing, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_bad_rdc_003_emits_sync_crossing() -> None:
    """The RDC-003 bad fixture has a sync reset crossing clock
    domains. ``find_reset_crossings`` must classify at least one
    such crossing as ``"sync-crossing"``."""
    module, clock_domains, _ = _load("bad_rdc_003_sync_reset_crossing")
    crossings = find_reset_crossings(module, clock_domains)
    kinds = {c.kind for c in crossings}
    assert "sync-crossing" in kinds, (
        f"expected a sync-crossing crossing, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_polarity_override_emits_polarity_mismatch() -> None:
    """Supplying a ``polarity_overrides`` map that disagrees with a
    flop's inferred polarity must emit a ``"polarity-mismatch"``
    crossing — independent of any crossing-kind that would also
    fire."""
    module, clock_domains, _ = _load("bad_marked_reset_polarity")
    crossings = find_reset_crossings(
        module,
        clock_domains,
        polarity_overrides={"rst_n": "low"},
    )
    kinds = {c.kind for c in crossings}
    assert "polarity-mismatch" in kinds, (
        f"expected polarity-mismatch, got kinds={kinds}, "
        f"crossings={[(c.flop, c.kind) for c in crossings]}"
    )


def test_polarity_override_quiet_when_matching() -> None:
    """A matching polarity declaration must NOT emit a mismatch."""
    module, clock_domains, _ = _load("good_marked_reset_polarity")
    crossings = find_reset_crossings(
        module,
        clock_domains,
        polarity_overrides={"rst_n": "low"},
    )
    assert all(c.kind != "polarity-mismatch" for c in crossings), (
        f"unexpected polarity-mismatch on matching fixture: "
        f"{[(c.flop, c.kind) for c in crossings]}"
    )


# --- every async control, not only ``ResetDomain.reset`` (rtl-buddy-cdc#390) ---


def _register_of(module, cell_name: str) -> str:
    """The named register a flop cell drives, so the ``proc`` and the
    gate-level form name the same flop the same way."""
    q = module.cells[cell_name].connections["Q"][0]
    for nn in sorted(
        module.netnames.values(), key=lambda n: (n.name in module.ports, n.name)
    ):
        if "$" not in nn.name and q in nn.bits:
            return nn.name
    return cell_name


def _crossings_by_register(fixture: str, form: str) -> set[tuple[str, str, str, str]]:
    dir_ = FIX_ROOT / fixture
    module = netlist.load(dir_ / f"{fixture}.{form}")
    spec = sdc_mod.parse_file(dir_ / f"{fixture}.sdc")
    clock_domains = {
        fd.flop.cell.name: fd.clock
        for fd in assign_domains(module, pin_clocks=spec.pin_clocks)
    }
    syncs = find_reset_synchronizers(module, clock_domains)
    return {
        (
            _register_of(module, c.flop),
            c.kind,
            str(c.pin),
            _register_of(module, c.reset.name)
            if c.reset.source == "inferred"
            else c.reset.source,
        )
        for c in find_reset_crossings(module, clock_domains, recognised_syncs=syncs)
    }


@pytest.mark.parametrize("form", ["json", "synth.json"])
def test_set_pin_crossings(form: str) -> None:
    """``bad_set_clear_foreign_controls``: qa's SET is a src_clk register
    and qb's SET a mux, while both flops' CLR is the clean ``rst_n``
    port. Only qc's CLR was seen when the walk read ``ResetDomain.reset``
    alone. Each record names the pin it is on."""
    got = _crossings_by_register("bad_set_clear_foreign_controls", form)
    assert got == {
        ("qa", "async-deassert", "SET", "set_q"),
        ("qb", "comb-driven", "SET", "comb"),
        ("qc", "async-deassert", "CLR", "clr_q"),
    }


@pytest.mark.parametrize("form", ["json", "synth.json"])
def test_set_and_aload_pin_crossings(form: str) -> None:
    got = _crossings_by_register("bad_sync_chain_foreign_set", form)
    assert got == {
        ("l1", "async-deassert", "ALOAD", "load_q"),
        ("l2", "async-deassert", "ALOAD", "load_q"),
        ("s1", "async-deassert", "SET", "set_q"),
        ("s2", "async-deassert", "SET", "set_q"),
    }


def test_single_reset_pin_is_named() -> None:
    """A one-reset flop's record names its pin too: ``ARST`` / ``SRST``."""
    module, clock_domains, _ = _load("bad_rdc_003_sync_reset_crossing")
    pins = {(c.kind, c.pin) for c in find_reset_crossings(module, clock_domains)}
    assert ("sync-crossing", "SRST") in pins
    module, clock_domains, _ = _load("bad_reset_crossing")
    pins = {(c.kind, c.pin) for c in find_reset_crossings(module, clock_domains)}
    assert ("async-deassert", "ARST") in pins
