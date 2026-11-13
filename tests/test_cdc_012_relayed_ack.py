"""CDC-012 recognises an acknowledge relayed through a third clock domain
(rtl-buddy-cdc#359).

``good_handshake_ack_via_third_domain``: the payload crosses clk_a →
clk_b (gated), and clk_b's acknowledge returns clk_b → clk_c (2FF,
re-registered) → clk_a (2FF), where it releases the held payload. The
feedback walk used to stay inside the source domain and stopped at the
first clk_c flop, so the handshake read as missing.

The walk may now continue through flops of other domains, but only along
the pins a value flows through (``D``, enable, synchronous reset) — so a
path that merely shares a reset, or a relay flop that never hears from
the destination, still leaves the crossing unacknowledged. These tests
build those shapes by rewiring the good fixture's clk_c relay.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.rules import _build_context, check_cdc_012, run_all

FIX = Path(__file__).parent / "fixtures"
GOOD = "good_handshake_ack_via_third_domain"
BAD = "bad_handshake_ack_via_third_domain"


def _raw(name: str) -> dict:
    return json.loads((FIX / name / f"{name}.json").read_text())


def _cell_by_q(mod: dict, q_net: str) -> dict:
    bits = mod["netnames"][q_net]["bits"]
    (cell,) = [c for c in mod["cells"].values() if c["connections"].get("Q") == bits]
    return cell


def _run(tmp_path: Path, raw: dict, name: str = GOOD) -> list[str]:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(raw))
    module = netlist.load(path)
    spec = sdc.parse_file(FIX / name / f"{name}.sdc", backend="tokenizer")
    sdc.synthesize_unconstrained_inputs(spec, module)
    crossings = filter_async(
        find_crossings(module, port_clock=spec.port_clock, **spec.clock_root_inputs()),
        spec,
    )
    return sorted(v.rule_id for v in run_all(module, crossings, spec))


def test_relayed_ack_is_feedback(tmp_path: Path) -> None:
    assert _run(tmp_path, _raw(GOOD)) == []


def test_bad_relay_reports_the_unsynchronised_hop_only(tmp_path: Path) -> None:
    assert _run(tmp_path, _raw(BAD), BAD) == ["CDC-001"]


def test_relay_that_only_shares_a_reset_is_not_an_ack(tmp_path: Path) -> None:
    """The clk_c relay's first flop hears clk_b's ack on its async reset
    pin only (its ``D`` is a constant): a reset path, not an acknowledge."""
    raw = copy.deepcopy(_raw(GOOD))
    mod = raw["modules"][GOOD]
    c_ack_s1 = _cell_by_q(mod, "c_ack_s1")
    c_ack_s1["connections"]["ARST"] = mod["netnames"]["b_ack_q"]["bits"]
    c_ack_s1["connections"]["D"] = ["0"]
    assert "CDC-012" in _run(tmp_path, raw)


def test_relay_that_never_hears_the_destination_is_not_an_ack(
    tmp_path: Path,
) -> None:
    """A free-running clk_c register (``D`` from its own ``Q``) feeding
    the source's hold logic is a third-domain path, but not from clk_b."""
    raw = copy.deepcopy(_raw(GOOD))
    mod = raw["modules"][GOOD]
    c_ack_s1 = _cell_by_q(mod, "c_ack_s1")
    c_ack_s1["connections"]["D"] = mod["netnames"]["c_ack_s1"]["bits"]
    assert "CDC-012" in _run(tmp_path, raw)


@pytest.mark.parametrize(
    "fixture",
    [
        "bad_handshake_ack_missing",
        "bad_functional_datahold_enable",
        "bad_mixed_handshake_datahold",
    ],
)
def test_existing_cdc_012_findings_still_fire(fixture: str) -> None:
    module = netlist.load(FIX / fixture / f"{fixture}.json")
    spec = sdc.parse_file(FIX / fixture / f"{fixture}.sdc", backend="tokenizer")
    sdc.synthesize_unconstrained_inputs(spec, module)
    crossings = filter_async(
        find_crossings(module, port_clock=spec.port_clock, **spec.clock_root_inputs()),
        spec,
    )
    assert check_cdc_012(module, crossings, spec, ctx=_build_context(module, spec))
