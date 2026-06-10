"""`(* cdc_handshake *)` opt-in suppression (issue #247).

The analyzer checks CDC *structure*, so a correct four-phase req/ack
handshake (the `ip_cdc_handshake` primitive) trips four rules on its
protected paths: CDC-020 (sliced payload reconvergence), CDC-013
(fast→slow toggle event-loss), CDC-001 (single-stage dst capture), and
CDC-014 (post-capture decode comb). All are safe by protocol. Tagging
the participating registers with `(* cdc_handshake *)` retires the
per-instance waivers.

These tests reuse the existing negative fixtures that each fire exactly
one of the four rules, then inject the attribute onto the offending
flop's Q net (in-memory, mirroring how Yosys surfaces the attribute on a
netname) and assert the finding disappears — exercising the full
parse → context → rule-skip path. A parsing unit test pins the
netname → flop mapping and the alias set directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import (
    USER_HANDSHAKE_ATTRS,
    run_all as run_all_rules,
    user_handshake_flop_names,
)

FIX = Path(__file__).parent / "fixtures"

# Each fixture fires exactly the named rule on the keyed flop; the rule
# keys the suppression on that same flop (dst capture / src toggle /
# chain head / sliced source), so tagging it must silence the finding.
_CASES = [
    ("CDC-001", "bad_single_ff_sync"),
    ("CDC-013", "bad_toggle_no_xor_tail"),
    ("CDC-014", "bad_comb_between_sync_stages"),
    ("CDC-020", "bad_sliced_bus_reconvergence"),
]


def _load(fixture: str):
    base = FIX / fixture / fixture
    json_path = base.with_suffix(".json")
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(base.with_suffix(".sdc"))
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = [
        c
        for c in crossings
        if not spec.is_unreachable_crossing(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
        and spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings, spec


def _tag_handshake(module: netlist.Module, cell_name: str, attr: str) -> None:
    """Annotate ``cell_name``'s Q net with a handshake attribute, the way
    a `(* cdc_handshake *)` declaration surfaces on a Yosys netname."""
    q_bits = module.cells[cell_name].connections.get("Q", ())
    module.netnames[f"_hs_tag_{cell_name}"] = netlist.Netname(
        name=f"_hs_tag_{cell_name}",
        bits=tuple(q_bits),
        attributes={attr: "true"},
    )


@pytest.mark.parametrize("rule_id,fixture", _CASES)
def test_handshake_attribute_suppresses(rule_id: str, fixture: str) -> None:
    module, async_crossings, spec = _load(fixture)

    before = run_all_rules(module, async_crossings, spec)
    fired = [v for v in before if v.rule_id == rule_id]
    assert len(fired) == 1, (
        f"fixture {fixture} should fire one {rule_id}; got "
        f"{[(v.rule_id, v.cell_name) for v in before]}"
    )

    _tag_handshake(module, fired[0].cell_name, "cdc_handshake")

    after = run_all_rules(module, async_crossings, spec)
    assert not [v for v in after if v.rule_id == rule_id], (
        f"{rule_id} should be suppressed once its flop is tagged "
        f"(* cdc_handshake *); still got "
        f"{[(v.rule_id, v.cell_name) for v in after]}"
    )


def test_handshake_tag_is_targeted_not_blanket() -> None:
    """Tagging one flop suppresses only the finding keyed at that flop —
    an untagged firing fixture is unchanged (no accidental blanket
    suppression from an empty handshake set)."""
    module, async_crossings, spec = _load("bad_single_ff_sync")
    assert user_handshake_flop_names(module) == set()  # nothing tagged yet
    before = run_all_rules(module, async_crossings, spec)
    assert any(v.rule_id == "CDC-001" for v in before)


def test_user_handshake_flop_names_maps_netname_to_flop() -> None:
    """`user_handshake_flop_names` resolves a tagged netname back to the
    flop cell whose Q it names, for both the canonical attribute and an
    alias; an unrelated attribute is ignored."""
    module, _crossings, _spec = _load("bad_single_ff_sync")
    target = next(
        v.cell_name
        for v in run_all_rules(module, _crossings, _spec)
        if v.rule_id == "CDC-001"
    )

    assert "cdc_handshake" in USER_HANDSHAKE_ATTRS
    assert "req_ack_handshake" in USER_HANDSHAKE_ATTRS

    # Canonical attribute.
    _tag_handshake(module, target, "cdc_handshake")
    assert target in user_handshake_flop_names(module)

    # Alias spelling resolves the same way.
    module2, _c2, _s2 = _load("bad_single_ff_sync")
    _tag_handshake(module2, target, "req_ack_handshake")
    assert target in user_handshake_flop_names(module2)

    # An unrelated attribute must not register as a handshake.
    module3, _c3, _s3 = _load("bad_single_ff_sync")
    _tag_handshake(module3, target, "some_other_attr")
    assert user_handshake_flop_names(module3) == set()
