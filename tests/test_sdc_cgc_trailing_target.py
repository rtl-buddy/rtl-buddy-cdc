"""Issue #140 end-to-end regression net.

Re-uses the ``good_source_sync_internal`` netlist (committed .json,
no Yosys at test time) but feeds it an SDC rewritten to the buggy
``create_generated_clock ... -source [...] [target]`` shape — the
form where ``-source`` is the trailing flag before the positional
target collection.

On the unpatched parser the trailing target is silently swallowed,
so ``pin_clocks`` is empty and ``trace_clock_root`` walks past each
forwarded-clock pin to ``ck_a``. The fixture then degenerates into
"every flop is on ck_a", which is structurally invisible (zero
cross-domain pairs) — i.e. the analyzer produces a *different*
view of the same design from the canonical SDC. With the #141 fix
the trailing-target shape is parsed identically to the canonical
flag-interleaved form, so analyzer output matches the original
``good_source_sync_internal`` results.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_crossings

JSON = (
    Path(__file__).parent
    / "fixtures"
    / "good_source_sync_internal"
    / "good_source_sync_internal.json"
)

# Identical semantics to good_source_sync_internal.sdc but with
# ``-source`` placed as the trailing flag before the positional
# target — the shape called out in issue #140.
TRAILING_TARGET_SDC = """
create_clock -name ck_a -period 10.0 [get_ports ck_a]

create_generated_clock -name ck_b0 -master_clock ck_a \\
    -source [get_ports ck_a] [get_pins u_a/clk_out_b0]
create_generated_clock -name ck_b1 -master_clock ck_a \\
    -source [get_ports ck_a] [get_pins u_a/clk_out_b1]
create_generated_clock -name ck_c0 -master_clock ck_b0 \\
    -source [get_pins u_a/clk_out_b0] [get_pins u_b0/clk_out]
create_generated_clock -name ck_c1 -master_clock ck_b1 \\
    -source [get_pins u_a/clk_out_b1] [get_pins u_b1/clk_out]

set_input_delay -clock ck_a 0.0 [get_ports d_in]
"""

EXPECTED_PIN_CLOCKS = {
    "u_a/clk_out_b0": "ck_b0",
    "u_a/clk_out_b1": "ck_b1",
    "u_b0/clk_out": "ck_c0",
    "u_b1/clk_out": "ck_c1",
}


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture netlist not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse(TRAILING_TARGET_SDC)
    return module, spec


def test_trailing_target_sdc_populates_pin_clocks(context) -> None:
    """The four pin-targeted generated clocks land in ``pin_clocks``
    even though their declarations put ``-source`` last."""
    _module, spec = context
    assert spec.pin_clocks == EXPECTED_PIN_CLOCKS


def test_trailing_target_sdc_resolves_to_ck_a(context) -> None:
    """Every generated clock collapses to ck_a's master — the same
    invariant the canonical good fixture relies on."""
    _module, spec = context
    for c in ("ck_b0", "ck_b1", "ck_c0", "ck_c1"):
        assert spec.resolve(c) == "ck_a", f"{c} did not resolve to ck_a"


def test_trailing_target_sdc_produces_four_block_domains(context) -> None:
    """Pin-clock retention is load-bearing for ``trace_clock_root``:
    each forwarded-clock pin must stop the walk and adopt the
    generated clock's identity, so flops downstream of u_a, u_b0,
    u_b1 carry distinct clock roots. With the bug the walk continues
    past the pin to ck_a and every flop collapses onto a single
    domain."""
    module, spec = context
    domains = assign_domains(module, pin_clocks=spec.pin_clocks)
    clock_roots = {fd.clock for fd in domains}
    assert clock_roots == {"ck_a", "ck_b0", "ck_b1", "ck_c0", "ck_c1"}


def test_trailing_target_sdc_emits_canonical_crossings(context) -> None:
    """Same crossing inventory as the canonical good fixture: four
    structural pairs (ck_a→ck_b0, ck_a→ck_b1, ck_b0→ck_c0,
    ck_b1→ck_c1), zero async after master collapse."""
    module, spec = context
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    pairs = {(c.src_clock, c.dst_clock) for c in crossings}
    assert pairs == {
        ("ck_a", "ck_b0"),
        ("ck_a", "ck_b1"),
        ("ck_b0", "ck_c0"),
        ("ck_b1", "ck_c1"),
    }
    async_pairs = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    assert async_pairs == []
