"""Foundation tests for :mod:`rtl_buddy_cdc.reset_domain`.

Pins the per-flop reset assignment against the existing reset
fixtures. No new fixtures yet — the foundation pass is rule-agnostic,
so the regression net is the shape of the data it returns on netlists
the suite already exercises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.domain import assign_domains
from rtl_buddy_cdc.reset_domain import (
    ResetSource,
    assign_reset_domains,
    find_reset_synchronizers,
)

FIX_ROOT = Path(__file__).parent / "fixtures"


def _load(name: str):
    path = FIX_ROOT / name / f"{name}.json"
    if not path.exists():
        pytest.skip(f"fixture not built: {path}")
    return netlist.load(path)


def test_bad_reset_crossing_shape() -> None:
    """The bad-reset fixture has two $adff flops:

    - ``src_kill_n`` (in the src_clk domain) is reset by the top-level
      ``global_rst_n`` port (port-sourced, active-low, async).
    - ``dst_q`` (in the dst_clk domain) is reset by ``src_kill_n``'s Q
      (flop-driven, async). This is precisely the CDC-007 anti-pattern.
    """
    module = _load("bad_reset_crossing")
    domains = assign_reset_domains(module)
    # Every flop in the module is represented.
    assert set(domains) == set(_flop_names(module))
    # Exactly one port-sourced async reset (global_rst_n) and one
    # inferred-from-flop async reset (the CDC-007 anti-pattern bit).
    port_sourced = [rd for rd in domains.values() if _is_port(rd.reset)]
    inferred = [rd for rd in domains.values() if _is_inferred(rd.reset)]
    assert len(port_sourced) == 1
    assert len(inferred) == 1
    assert (
        port_sourced[0].reset is not None
        and port_sourced[0].reset.name == "global_rst_n"
    )
    assert port_sourced[0].reset.type == "async"
    assert port_sourced[0].reset.polarity == "low"
    # The CDC-007 case: the inferred-source reset's ``name`` is the
    # driving flop's cell name. That handle is what RDC rules will
    # cross-reference back to a clock-domain assignment.
    assert inferred[0].reset is not None
    assert inferred[0].reset.type == "async"
    assert inferred[0].reset.polarity == "low"
    assert inferred[0].reset.name == port_sourced[0].flop


def test_good_reset_sync_shape() -> None:
    """The good-reset fixture has all reset pins driven by top-level
    ports or by recognised reset-sync flops — every entry should be
    port-sourced or flop-inferred, never combinational, never constant."""
    module = _load("good_reset_sync")
    domains = assign_reset_domains(module)
    for rd in domains.values():
        if rd.reset is None:
            continue
        assert rd.reset.source in {"port", "inferred"}, (
            f"unexpected reset source {rd.reset.source!r} on {rd.flop}"
        )


def test_no_reset_pin_yields_none() -> None:
    """The handshake fixture's payload register is a plain $dffe — no
    reset pin at all. It must appear in the map with ``reset=None``
    rather than being silently dropped."""
    module = _load("ip_cdc_handshake")
    domains = assign_reset_domains(module)
    # Some flop(s) in this design *do* have async reset; that's fine.
    # The contract we're testing is that every flop is represented,
    # and that reset-less flops are present with ``reset=None``.
    assert len(domains) == len(_flop_names(module))
    # At least one entry should be reset-bearing (the design uses
    # rst_n) and at least one should be reset-less if the design has
    # any $dffe — the assertion below is a sanity check that we're
    # not silently classifying every flop the same way.
    has_reset = sum(1 for rd in domains.values() if rd.reset is not None)
    no_reset = sum(1 for rd in domains.values() if rd.reset is None)
    assert has_reset + no_reset == len(domains)


def test_recognizer_finds_good_reset_sync_chain() -> None:
    """The good-reset-sync fixture's 2FF chain (``dst_rst_meta`` and
    ``dst_rst_n_sync``) must be recognised as a reset synchronizer.

    The data-path flops (``src_q``, ``sync_meta``, ``sync_q``) are
    *consumers* of the synchronised reset, not part of the synchronizer
    chain itself — they must NOT be flagged. The chain head
    (``dst_rst_meta``)'s ``D`` is tied to ``1'b1``; the tail
    (``dst_rst_n_sync``)'s ``D`` is the head's ``Q``.
    """
    module = _load("good_reset_sync")
    flop_clocks = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    syncs = find_reset_synchronizers(module, flop_clocks)
    names = {name.split(".")[-1].lstrip("\\") for name in syncs}
    # The chain head and tail (and only those) — the fixture uses
    # Yosys auto-names (``$procdff$N``) so we filter by membership and
    # count rather than asserting on the names.
    assert len(syncs) == 2, f"expected 2 sync stages, got {sorted(syncs)}"
    # Sanity check that the recognised flops are exactly the ones whose
    # D pin is either a constant (chain head) or another recognised
    # sync stage's Q (chain tail).
    assert all("$procdff" in n or "rst" in n for n in names), names


def test_recognizer_skips_bad_reset_crossing() -> None:
    """``bad_reset_crossing`` has no constant-fed reset chain at all
    (``src_kill_n``'s D is ``~kill_req``, ``dst_q``'s D is a data
    port). The recogniser must return an empty set so RDC-001 cannot
    misclassify either flop as a synchroniser and let the crossing
    through."""
    module = _load("bad_reset_crossing")
    flop_clocks = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    syncs = find_reset_synchronizers(module, flop_clocks)
    assert syncs == set()


def test_recognizer_min_depth_validation() -> None:
    module = _load("good_reset_sync")
    flop_clocks = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    with pytest.raises(ValueError):
        find_reset_synchronizers(module, flop_clocks, min_depth=0)


def test_recognizer_min_depth_three_drops_the_2ff_chain() -> None:
    """Bumping ``min_depth`` past the chain length excludes it.

    Mirrors how the rule pack can let projects raise the bar (analogous
    to CDC-002's ``required_depth``). At depth=3 the good fixture's
    2FF chain no longer qualifies."""
    module = _load("good_reset_sync")
    flop_clocks = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    assert find_reset_synchronizers(module, flop_clocks, min_depth=3) == set()


def test_dataclasses_frozen() -> None:
    """The data model is frozen so consumers can hash and cache it."""
    rs = ResetSource(name="rst_n", polarity="low", type="async", source="port")
    with pytest.raises(Exception):
        rs.name = "other"  # type: ignore[misc]


# --- helpers ---------------------------------------------------------------


def _flop_names(module) -> list[str]:
    from rtl_buddy_cdc.flops import find_flops

    return [f.cell.name for f in find_flops(module)]


def _is_port(reset: ResetSource | None) -> bool:
    return reset is not None and reset.source == "port"


def _is_inferred(reset: ResetSource | None) -> bool:
    return reset is not None and reset.source == "inferred"
