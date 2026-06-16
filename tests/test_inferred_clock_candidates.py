"""P3 inferred-clocks (issue #263) — advisory detection of undeclared
internal generated clocks, with a hard no-reclassification guarantee.

The P3 change reports internal nets that fan out to many flop ``CLK``
pins but carry no declared clock identity — a likely-forgotten
``create_generated_clock``. It is ADVISORY ONLY:

  - JSON ``inferred_clock_candidates`` lists each candidate (driver cell,
    driver kind, CLK-pin fanout, bounded sink sample).
  - The text report emits a cyan ``ⓘ`` advisory line.
  - It NEVER changes a flop's domain, a crossing, or a violation. The
    fanout heuristic alone never assigns a domain; a flop behind such a
    net resolves only when a real clock-root trace already follows it
    (the divider/latch clause of ``trace_clock_root``).

The fixture ``inferred_fwd_clock`` is a divide-by-2 toggle flop whose Q
clocks a four-flop bank with NO ``create_generated_clock`` in the SDC.
The toggle flop is flagged as a candidate; the bank flops still resolve
to ``clk_a`` via the divider trace (a real trace, not the heuristic). It
also carries a genuine ``clk_a -> clk_b`` async crossing as a parity
anchor.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import OutputFormat, _analyze_and_report  # noqa: PLC2701
from rtl_buddy_cdc.domain import (
    assign_domains,
    find_crossings,
    find_inferred_clock_candidates,
)
from rtl_buddy_cdc.reporter import (
    JSON_CONTRACT,
    AnalysisResult,
    render_json,
    render_text,
)
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "inferred_fwd_clock"
JSON = FIX_DIR / "inferred_fwd_clock.json"
SDC = FIX_DIR / "inferred_fwd_clock.sdc"


def _skip_if_missing() -> None:
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")


def test_candidate_is_reported_in_json(tmp_path: Path) -> None:
    """The undeclared divide-by-2 net is flagged as an inferred-clock
    candidate: a flop-Q driver fanning out to the four-flop bank."""
    _skip_if_missing()
    out = tmp_path / "report.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    cands = payload["inferred_clock_candidates"]
    assert len(cands) == 1
    c = cands[0]
    assert c["driver_kind"] == "flop"
    assert c["fanout"] == 4
    # The sink sample names real flop cells and never exceeds the fanout.
    assert 1 <= len(c["example_sinks"]) <= c["fanout"]
    assert all(s in {d["flop"] for d in payload["domains"]} for s in c["example_sinks"])


def test_candidate_does_not_reclassify_any_flop(tmp_path: Path) -> None:
    """ADVISORY: the bank flops resolve to clk_a via the divider trace —
    a real clock-root trace, not the fanout heuristic. The advisory adds
    no domain and removes none; every sink the candidate names is still
    a resolved, real-domain flop."""
    _skip_if_missing()
    out = tmp_path / "report.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    # The candidate's sinks resolve to clk_a via the divider trace, so
    # they are NOT domain-unknown — the advisory did not invent that.
    domain_by_flop = {d["flop"]: d["clock"] for d in payload["domains"]}
    cand = payload["inferred_clock_candidates"][0]
    for sink in cand["example_sinks"]:
        assert domain_by_flop[sink] == "clk_a"
    assert payload["summary"]["domain_unknown"] == 0


def _from_scratch(json_path: Path, sdc_path: Path) -> tuple[int, int, list[str]]:
    """Analyse a fixture WITHOUT the inferred-clock feature — the ground-
    truth crossing/violation set the advisory must not perturb. Returns
    (n_crossings, n_violations, sorted per-flop domain assignments)."""
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    domains = assign_domains(
        module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
    )
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
    dom = sorted(f"{d.flop.cell.name}={d.clock}" for d in domains)
    return len(crossings), len(violations), dom


def test_parity_no_crossing_or_domain_changed(tmp_path: Path) -> None:
    """PARITY (the core correctness guarantee): the advisory is purely
    additive. The full ``analyze`` path's crossing/violation counts AND
    the exact per-flop domain assignments match a from-scratch analysis
    that never computes a candidate — proving NO crossing, violation, or
    domain assignment was altered by the inference being reported."""
    _skip_if_missing()
    plain_crossings, plain_violations, plain_domains = _from_scratch(JSON, SDC)
    out = tmp_path / "report.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, out)
    payload = json.loads(out.read_text())
    assert payload["summary"]["crossings"] == plain_crossings
    assert payload["summary"]["violations"] == plain_violations
    feature_domains = sorted(f"{d['flop']}={d['clock']}" for d in payload["domains"])
    assert feature_domains == plain_domains
    # The parity anchor is non-vacuous: there is a real async crossing.
    assert plain_crossings >= 1
    assert plain_violations >= 1
    # And the advisory genuinely fired (otherwise this proves nothing).
    assert payload["inferred_clock_candidates"]


def test_declared_generated_clock_is_not_flagged() -> None:
    """A net the user DID declare with ``create_generated_clock`` (in the
    SDC ``pin_clocks`` map) is not a forgotten clock and must not appear
    as a candidate. We synthesise the pin-clock map naming the toggle
    flop's output net and confirm the candidate disappears."""
    _skip_if_missing()
    module = netlist.load(JSON)
    # With no pin_clocks the toggle flop is a candidate.
    base = find_inferred_clock_candidates(module)
    assert base, "fixture should produce a candidate without pin_clocks"
    driver = base[0].driver
    # Find the net name carrying the toggle flop's Q so we can pretend the
    # SDC declared a generated clock there.
    q_bits = set(module.cells[driver].connections.get("Q", ()))
    declared_net = next(nn for nn, n in module.netnames.items() if q_bits & set(n.bits))
    suppressed = find_inferred_clock_candidates(
        module, pin_clocks={declared_net: "clk_div"}
    )
    assert all(c.driver != driver for c in suppressed)


def test_threshold_floor_is_respected() -> None:
    """Raising the threshold above the fixture's fanout silences the
    candidate — the report is tunable and conservative by default."""
    _skip_if_missing()
    module = netlist.load(JSON)
    assert find_inferred_clock_candidates(module, threshold=4)
    assert find_inferred_clock_candidates(module, threshold=5) == []


def test_inferred_clock_candidates_not_in_json_contract() -> None:
    """The advisory is additive, NOT a pinned downstream contract key —
    it must not appear in JSON_CONTRACT (downstream parses only the three
    stable summary counts + domain_unknown)."""
    assert "inferred_clock_candidates" not in JSON_CONTRACT
    assert "summary.inferred_clock_candidates" not in JSON_CONTRACT


def test_text_report_emits_advisory_line(tmp_path: Path) -> None:
    """The text report carries the ⓘ advisory naming the driver and its
    CLK-pin fanout."""
    _skip_if_missing()
    out = tmp_path / "report.txt"
    _analyze_and_report(JSON, SDC, None, OutputFormat.text, out, color=False)
    text = out.read_text()
    assert "undeclared internal clock candidate" in text
    assert "create_generated_clock" in text


def test_no_candidate_stays_quiet() -> None:
    """A result with no candidate emits an empty list and no advisory
    line — the report stays silent when there is nothing to flag."""
    _skip_if_missing()
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    domains = assign_domains(module)
    result = AnalysisResult(
        module=module,
        domains=domains,
        crossings=[],
        async_crossings=[],
        spec=spec,
        violations=[],
        inferred_clock_candidates=[],
    )
    buf = io.StringIO()
    render_json(result, buf)
    payload = json.loads(buf.getvalue())
    assert payload["inferred_clock_candidates"] == []
    tbuf = io.StringIO()
    render_text(result, tbuf, color=False)
    assert "undeclared internal clock candidate" not in tbuf.getvalue()
