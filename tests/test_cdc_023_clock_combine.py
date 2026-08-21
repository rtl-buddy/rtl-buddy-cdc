"""CDC-023 — clock net driven by a combine of two declared clocks (#269).

Issue #263 taught the clock-root tracer to DECLINE when a gate or a
clock-path transparent latch combines two distinct declared clocks; the
downstream flop then landed in the generic ``domain_unknown`` bucket with
no cause attached. CDC-023 names that decline: it reports the combining
cell, the combined net, and the two clocks.

The rule is built on the tracer's own decline events
(``domain.find_clock_combines`` attaches a recorder to the ordinary
clock-root walk), so "the rule fired" and "the tracer declined" are the
SAME event rather than two predicates that happen to agree today. The
tests below pin both directions of that equivalence:

- the combine fixtures fire AND leave their flops domain-unknown;
- ``icg_port_enable`` (clock + untyped enable PORT) does neither, which
  is the #263 regression guard inherited for free — every fixture here
  is loaded through ``synthesize_unconstrained_inputs`` so the
  ``<unconstrained>`` sentinel really is in play (a bare
  ``assign_domains`` call would silently miss it);
- a clock MUX is not a combine — it selects — and never fires.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_clock_combines, find_crossings
from rtl_buddy_cdc.netlist import Module, Netname, Port
from rtl_buddy_cdc.rules import check_cdc_023, run_all as run_all_rules
from rtl_buddy_cdc.waivers import apply as apply_waivers, parse as parse_waivers

FIX = Path(__file__).parent / "fixtures"


def _load(name: str):
    """Load a fixture the way the CLI does.

    ``synthesize_unconstrained_inputs`` is NOT optional here: it stamps
    the ``<unconstrained>`` sentinel on every untyped input port, and the
    combine predicate has to keep treating that sentinel as "not a
    clock". Skipping it tests the wrong path — the exact gotcha the #267
    hardening hit.
    """
    d = FIX / name
    json_path = d / f"{name}.json"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(d / f"{name}.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    return module, spec


def _findings(name: str):
    module, spec = _load(name)
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    violations = run_all_rules(module, crossings, spec)
    return [v for v in violations if v.rule_id == "CDC-023"], violations


def _unresolved(name: str) -> set[str]:
    module, spec = _load(name)
    return {
        fd.flop.cell.name
        for fd in assign_domains(
            module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
        )
        if fd.clock is None
    }


# --- the combine fixtures fire -------------------------------------------


@pytest.mark.parametrize(
    "fixture,clocks",
    [
        # $and with A=clkA, B=clkB.
        ("clock_combine_gate", ("clkA", "clkB")),
        # $dlatch with D=clkA, EN=clkB.
        ("clock_combine_latch", ("clkA", "clkB")),
        # A DECLARED generated clock combined with a real clock: a
        # create_generated_clock target is a full clock identity.
        ("clock_combine_generated", ("clkB", "gdiv")),
    ],
)
def test_combine_fires_once(fixture: str, clocks: tuple[str, str]) -> None:
    fired, _ = _findings(fixture)
    assert len(fired) == 1, [v.message for v in fired]
    v = fired[0]
    assert v.severity == "warning"
    # The finding must name the cell and BOTH clocks — the whole point is
    # that the user can act on it without hunting the domain_unknown list.
    assert v.cell_name
    assert f"{{{clocks[0]}, {clocks[1]}}}" in v.message
    assert f"`{v.cell_name}`" in v.message
    assert "CLK net `gclk`" in v.message


@pytest.mark.parametrize(
    "fixture", ["clock_combine_gate", "clock_combine_latch", "clock_combine_generated"]
)
def test_combine_is_the_only_rule_that_fires(fixture: str) -> None:
    """These fixtures are self-contained: the combine is the only smell."""
    _, violations = _findings(fixture)
    assert sorted({v.rule_id for v in violations}) == ["CDC-023"], [
        v.rule_id for v in violations
    ]


@pytest.mark.parametrize(
    "fixture", ["clock_combine_gate", "clock_combine_latch", "clock_combine_generated"]
)
def test_finding_matches_the_decline(fixture: str) -> None:
    """rule-fires ⟺ decline-happened.

    Every flop a reported combine reaches must be exactly a flop the
    tracer left domain-unknown. This is what the shared recorder buys:
    the two views cannot drift.
    """
    module, spec = _load(fixture)
    combines = find_clock_combines(
        module, spec.pin_clocks, clock_for_port=spec.clock_for_port
    )
    assert combines
    reached = {s for c in combines for s in c.sinks}
    assert reached
    assert reached <= _unresolved(fixture)


def test_gate_and_latch_report_their_cell_type() -> None:
    """Both combine shapes are covered — a gate and a transparent latch."""
    module, spec = _load("clock_combine_gate")
    (gate,) = find_clock_combines(
        module, spec.pin_clocks, clock_for_port=spec.clock_for_port
    )
    assert gate.cell_type == "$and"

    module, spec = _load("clock_combine_latch")
    (latch,) = find_clock_combines(
        module, spec.pin_clocks, clock_for_port=spec.clock_for_port
    )
    assert latch.cell_type == "$dlatch"


# --- what must NOT fire ---------------------------------------------------


def test_icg_port_enable_does_not_fire() -> None:
    """REGRESSION GUARD (#263 → #269): a normal ICG is clock + enable.

    ``en`` is an untyped input port, so ``synthesize_unconstrained_inputs``
    stamps it ``<unconstrained>``. That sentinel is not a clock identity;
    if it ever counted as one, this fixture would both wrongly decline
    and wrongly fire CDC-023.
    """
    fired, _ = _findings("icg_port_enable")
    assert fired == []
    assert _unresolved("icg_port_enable") == set()


@pytest.mark.parametrize(
    "fixture",
    [
        # A mux SELECTS between two clocks; it does not combine them, so
        # it is deliberately outside the decline (and outside CDC-023).
        "bad_async_clock_mux",
        "good_exclusive_clock_mux",
        # A single clock routed through a latch resolves normally.
        "clock_through_latch",
        # A divider chain: one clock identity all the way down.
        "good_generated_clock_div2",
    ],
)
def test_non_combining_clock_shapes_stay_silent(fixture: str) -> None:
    fired, _ = _findings(fixture)
    assert fired == [], [v.message for v in fired]


def test_no_sdc_means_no_findings() -> None:
    """With no SDC there are no declared clock identities to combine."""
    module, _ = _load("clock_combine_gate")
    assert check_cdc_023(module, [], None) == []


def test_single_declared_clock_skips_the_walk() -> None:
    """One clock cannot combine with itself — the walk is skipped."""
    module, spec = _load("icg_port_enable")
    assert (
        find_clock_combines(module, spec.pin_clocks, clock_for_port=spec.clock_for_port)
        == []
    )


# --- waivers + reporting --------------------------------------------------


def test_waivable_by_rule_id() -> None:
    """Nothing special is needed: waivers key off the rule id."""
    fired, violations = _findings("clock_combine_gate")
    assert fired
    kept, suppressed = apply_waivers(
        violations, parse_waivers("waive CDC-023 .*  intentional clock chop")
    )
    assert [v.rule_id for v in kept] == []
    assert [s.violation.rule_id for s in suppressed] == ["CDC-023"]


def test_finding_survives_the_reporters() -> None:
    """Additive rule id — text / JSON / SARIF all carry it unchanged."""
    from rtl_buddy_cdc import reporter

    module, spec = _load("clock_combine_gate")
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    result = reporter.AnalysisResult(
        module=module,
        domains=assign_domains(
            module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
        ),
        crossings=crossings,
        async_crossings=crossings,
        spec=spec,
        violations=run_all_rules(module, crossings, spec),
    )
    text = io.StringIO()
    reporter.render_text(result, text)
    assert "CDC-023" in text.getvalue()

    buf = io.StringIO()
    reporter.render_json(result, buf)
    payload = json.loads(buf.getvalue())
    assert any(f["rule_id"] == "CDC-023" for f in payload["violations"])
    assert payload["summary"]["violations"] == 1

    buf = io.StringIO()
    reporter.render_sarif(result, buf)
    sarif = json.loads(buf.getvalue())
    assert any(
        r["id"] == "CDC-023"
        for r in sarif["runs"][0]["tool"]["driver"].get("rules", [])
    )


# --- message shaping ------------------------------------------------------


def _synthetic(bits_named_auto: bool) -> Module:
    """A hand-built netlist: one $and combining two clock ports, feeding
    four flop CLK pins. Exercises the >3-sink message sample and the
    auto-name (``$...``) preference in ``_bit_net_names``."""
    names = {
        "clkA": Netname(name="clkA", bits=(1,), attributes={}),
        "clkB": Netname(name="clkB", bits=(2,), attributes={}),
    }
    if bits_named_auto:
        names["$auto$gclk"] = Netname(name="$auto$gclk", bits=(3,), attributes={})
        names["gclk"] = Netname(name="gclk", bits=(3,), attributes={})
    cells = {
        "u_and": netlist.Cell(
            name="u_and",
            type="$and",
            connections={"A": (1,), "B": (2,), "Y": (3,)},
            parameters={},
            attributes={},
        ),
    }
    for i in range(4):
        cells[f"ff{i}"] = netlist.Cell(
            name=f"ff{i}",
            type="$dff",
            connections={"CLK": (3,), "D": (4 + i,), "Q": (8 + i,)},
            parameters={},
            attributes={},
        )
    return Module(
        name="synth",
        ports={
            "clkA": Port(name="clkA", direction="input", bits=(1,)),
            "clkB": Port(name="clkB", direction="input", bits=(2,)),
        },
        cells=cells,
        netnames=names,
    )


def _identity(root: str) -> str | None:
    return root if root in {"clkA", "clkB"} else None


def test_wide_fanout_message_truncates_the_sink_sample() -> None:
    module = _synthetic(bits_named_auto=True)
    (v,) = check_cdc_023(module, [], _FakeSpec())
    assert "4 flop(s)" in v.message
    assert "`ff0`, `ff1`, `ff2`, +1 more" in v.message
    # The user-written net name wins over the Yosys auto-generated one.
    assert "CLK net `gclk`" in v.message


def test_unnamed_combined_net_falls_back_to_the_cell() -> None:
    """A combined net with no netname entry still names its driver."""
    module = _synthetic(bits_named_auto=False)
    (combine,) = find_clock_combines(module, None, clock_for_port=_identity)
    assert combine.net == "u_and.Y"
    assert combine.sinks == ("ff0", "ff1", "ff2", "ff3")


class _FakeSpec:
    """Minimal ClockSpec stand-in for the synthetic netlist."""

    pin_clocks: dict[str, str] = {}

    @staticmethod
    def clock_for_port(name: str) -> str | None:
        return _identity(name)
