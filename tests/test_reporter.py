"""Reporter unit tests — text/json/sarif rendering of an AnalysisResult."""

from __future__ import annotations

import dataclasses
import importlib.metadata
import io
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import _analyze_and_report  # noqa: PLC2701
from rtl_buddy_cdc.cli import OutputFormat  # noqa: I001
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.reporter import (
    JSON_CONTRACT,
    TOOL_VERSION,
    AnalysisResult,
    _instance_path,
    render_json,
    render_sarif,
    render_text,
)
from rtl_buddy_cdc.rules import run_all as run_all_rules
from rtl_buddy_cdc.waivers import apply as apply_waivers, parse as parse_waivers

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_single_ff_sync"
JSON_PATH = FIX_DIR / "bad_single_ff_sync.json"
SDC_PATH = FIX_DIR / "bad_single_ff_sync.sdc"


@pytest.fixture(scope="module")
def result() -> AnalysisResult:
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    module = netlist.load(JSON_PATH)
    spec = sdc_mod.parse_file(SDC_PATH)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    violations = run_all_rules(module, async_crossings, spec)
    return AnalysisResult(
        module=module,
        domains=assign_domains(module),
        crossings=crossings,
        async_crossings=async_crossings,
        spec=spec,
        violations=list(violations),
    )


def test_text_includes_violation(result: AnalysisResult) -> None:
    buf = io.StringIO()
    render_text(result, buf)
    text = buf.getvalue()
    assert "bad_single_ff_sync" in text
    assert "CDC-001" in text
    # Message text may be wrapped across lines; collapse whitespace
    # before checking for key phrases.
    collapsed = " ".join(text.split())
    assert "no second-stage synchronizer" in collapsed
    assert "FAIL" in text


def test_json_is_well_formed(result: AnalysisResult) -> None:
    buf = io.StringIO()
    render_json(result, buf)
    data = json.loads(buf.getvalue())
    assert data["module"] == "bad_single_ff_sync"
    assert data["summary"]["violations"] == 1
    v = data["violations"][0]
    assert v["rule_id"] == "CDC-001"
    assert v["severity"] == "error"
    # Source location should be populated from the dst flop's src attr.
    assert v["location"]["file"].endswith("bad_single_ff_sync.sv")
    assert v["location"]["start_line"] >= 1


def test_sarif_minimum_shape(result: AnalysisResult) -> None:
    buf = io.StringIO()
    render_sarif(result, buf)
    data = json.loads(buf.getvalue())
    assert data["version"] == "2.1.0"
    runs = data["runs"]
    assert len(runs) == 1
    tool = runs[0]["tool"]["driver"]
    assert tool["name"] == "rtl-buddy-cdc"
    # Rule metadata for every distinct rule_id reported.
    assert {r["id"] for r in tool["rules"]} == {"CDC-001"}
    results = runs[0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "CDC-001"
    assert results[0]["level"] == "error"
    loc = results[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"].endswith("bad_single_ff_sync.sv")
    assert "region" in loc and loc["region"]["startLine"] >= 1


def test_cli_format_dispatch_to_file(tmp_path: Path) -> None:
    """`--format json --output <file>` writes valid JSON to the file."""
    out = tmp_path / "report.json"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, None, OutputFormat.json, out)
    assert code == 1  # one CDC-001 violation
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 1


# --- JSON contract tests (issue #13) ----------------------------------------
#
# These pin the cross-repo JSON contract documented in AGENTS.md
# "Cross-repo coupling". The downstream consumer (rtl_buddy via
# rtl_buddy/src/rtl_buddy/tools/cdc_rtl_buddy.py) only depends on the
# three keys named in ``JSON_CONTRACT``; everything else in the
# payload can evolve freely. Renaming or retyping any of these is a
# breaking change and must trip these tests.


def _walk_dotted(payload: dict, dotted_path: str) -> object:
    """Resolve ``"summary.violations"`` into ``payload["summary"]["violations"]``."""
    node: object = payload
    for part in dotted_path.split("."):
        assert isinstance(node, dict), (
            f"contract path {dotted_path!r}: expected dict at {part!r}, got {type(node).__name__}"
        )
        assert part in node, f"contract path {dotted_path!r}: missing key {part!r}"
        node = node[part]
    return node


def test_json_contract_keys_present_and_typed(result: AnalysisResult) -> None:
    """Every dotted key in ``JSON_CONTRACT`` must exist in the rendered
    payload and resolve to the declared type. This is the load-bearing
    test for the rtl-buddy ↔ rtl-buddy-cdc subprocess boundary; if it
    fails, downstream ``CdcResults`` parsing will start blowing up."""
    buf = io.StringIO()
    render_json(result, buf)
    payload = json.loads(buf.getvalue())
    for dotted, expected_type in JSON_CONTRACT.items():
        value = _walk_dotted(payload, dotted)
        assert isinstance(value, expected_type), (
            f"contract drift on {dotted!r}: expected {expected_type.__name__}, "
            f"got {type(value).__name__} ({value!r})"
        )


def test_json_contract_includes_waived_run(result: AnalysisResult) -> None:
    """``summary.violations`` decreases and ``summary.suppressed`` grows
    when a matching waiver is applied — exercising BOTH contract keys
    in one render. Without this, a regression that double-counts or
    swaps the two could pass the previous test (each key is still an
    int, just with the wrong value)."""
    waivers = parse_waivers("waive CDC-001 procdff\\$9 reviewed\n")
    kept, suppressed = apply_waivers(result.violations, waivers)
    waived_result = AnalysisResult(
        module=result.module,
        domains=result.domains,
        crossings=result.crossings,
        async_crossings=result.async_crossings,
        spec=result.spec,
        violations=list(kept),
        suppressed=list(suppressed),
    )
    buf = io.StringIO()
    render_json(waived_result, buf)
    payload = json.loads(buf.getvalue())
    # All contract keys still typed correctly under a waivered run.
    for dotted, expected_type in JSON_CONTRACT.items():
        value = _walk_dotted(payload, dotted)
        assert isinstance(value, expected_type), dotted
    # And specifically: the single CDC-001 violation moved from the
    # `violations` count to the `suppressed` count. This is the actual
    # downstream semantics rtl-buddy keys off.
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["suppressed"] == 1
    # The suppressed entry is preserved in the payload (not dropped)
    # so SARIF and JSON consumers can show the waived alert.
    assert len(payload["suppressed"]) == 1
    assert payload["suppressed"][0]["rule_id"] == "CDC-001"
    assert payload["suppressed"][0]["waiver"]["reason"] == "reviewed"


def test_sarif_suppression_shape(result: AnalysisResult) -> None:
    """SARIF emits suppressed findings with a ``suppressions`` field so
    GitHub Code Scanning (and any other SARIF consumer) knows the alert
    was intentionally hushed. The shape is fixed by SARIF 2.1.0 spec
    + our convention; no test currently pins it."""
    waivers = parse_waivers("waive CDC-001 procdff\\$9 reviewed by team\n")
    kept, suppressed = apply_waivers(result.violations, waivers)
    waived_result = AnalysisResult(
        module=result.module,
        domains=result.domains,
        crossings=result.crossings,
        async_crossings=result.async_crossings,
        spec=result.spec,
        violations=list(kept),
        suppressed=list(suppressed),
    )
    buf = io.StringIO()
    render_sarif(waived_result, buf)
    data = json.loads(buf.getvalue())
    results = data["runs"][0]["results"]
    # The suppressed finding is still emitted (not dropped) so the
    # alert exists; the suppressions field tells consumers not to fail
    # the build on it.
    assert len(results) == 1
    entry = results[0]
    assert entry["ruleId"] == "CDC-001"
    assert "suppressions" in entry, (
        "SARIF entry for a waived finding must carry a `suppressions` field"
    )
    supp = entry["suppressions"]
    assert isinstance(supp, list) and len(supp) == 1
    # Conventions: external waiver, accepted, with the user-provided
    # reason copied into justification.
    assert supp[0]["kind"] == "external"
    assert supp[0]["status"] == "accepted"
    assert supp[0]["justification"] == "reviewed by team"


def test_tool_version_matches_pyproject() -> None:
    """``reporter.TOOL_VERSION`` (the string SARIF puts in
    ``runs[0].tool.driver.version``) and the package version
    ``pyproject.toml`` declares must agree. They're bumped in
    lockstep on release per ``AGENTS.md``'s "Commit / branch /
    release conventions"; this sentinel catches a one-sided edit
    that would leave SARIF consumers pointing at the wrong wheel."""
    assert TOOL_VERSION == importlib.metadata.version("rtl-buddy-cdc")


# --- --strict severity promotion (issue #29) --------------------------------
#
# CDC-002 fires at warning severity. With ``--strict`` it should render
# as an error in every output format AND drive the same exit code (1 —
# any kept violation already drives it). The fixture is good_2ff_sync
# (depth-2 chain): silent at the default ``--sync-depth 2``, fires
# CDC-002 once at ``--sync-depth 3``.

_STRICT_FIX = Path(__file__).parent / "fixtures" / "good_2ff_sync"
_STRICT_JSON = _STRICT_FIX / "good_2ff_sync.json"
_STRICT_SDC = _STRICT_FIX / "good_2ff_sync.sdc"


def _strict_skip_if_missing() -> None:
    if not _STRICT_JSON.exists():
        pytest.skip(f"fixture not built: {_STRICT_JSON}")


def test_strict_promotes_cdc_002_in_text(tmp_path: Path) -> None:
    _strict_skip_if_missing()
    out = tmp_path / "report.txt"
    code = _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.text,
        out,
        sync_depth=3,
        strict=True,
    )
    assert code == 1
    text = out.read_text()
    assert "CDC-002" in text
    # Promoted: the rendered severity for this finding is now ``error``,
    # and the summary banner counts it as such.
    assert "error" in text
    assert "1 error" in text
    # And no stray ``warning`` token (the rule's natural severity).
    assert "warning" not in text


def test_strict_promotes_cdc_002_in_json(tmp_path: Path) -> None:
    _strict_skip_if_missing()
    out = tmp_path / "report.json"
    code = _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        out,
        sync_depth=3,
        strict=True,
    )
    assert code == 1
    payload = json.loads(out.read_text())
    cdc_002 = [v for v in payload["violations"] if v["rule_id"] == "CDC-002"]
    assert len(cdc_002) == 1
    assert cdc_002[0]["severity"] == "error"


def test_strict_promotes_cdc_002_in_sarif(tmp_path: Path) -> None:
    _strict_skip_if_missing()
    out = tmp_path / "report.sarif"
    code = _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.sarif,
        out,
        sync_depth=3,
        strict=True,
    )
    assert code == 1
    payload = json.loads(out.read_text())
    results = payload["runs"][0]["results"]
    cdc_002 = [r for r in results if r["ruleId"] == "CDC-002"]
    assert len(cdc_002) == 1
    # SARIF maps internal severity ``error`` → level ``error`` (warning
    # would have become ``warning``).
    assert cdc_002[0]["level"] == "error"


def test_strict_off_is_a_noop(tmp_path: Path) -> None:
    """Without ``--strict``, CDC-002 stays at its natural warning
    severity — promotion is opt-in, not a behaviour change."""
    _strict_skip_if_missing()
    out = tmp_path / "report.json"
    code = _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        out,
        sync_depth=3,
        strict=False,
    )
    assert code == 1  # CDC-002 is still a violation, just at warning level
    payload = json.loads(out.read_text())
    cdc_002 = [v for v in payload["violations"] if v["rule_id"] == "CDC-002"]
    assert len(cdc_002) == 1
    assert cdc_002[0]["severity"] == "warning"


# --- --baseline filter (issue #30) ------------------------------------------
#
# Acceptance-criteria cases: identical baseline → empty, baseline subset
# → only the new finding kept. The JSON payload exposes a
# ``baseline_carryover`` tally / list; SARIF emits carryover entries with
# a ``suppressions`` field tagged ``carried over from baseline``.


def _run_baseline_test(
    tmp_path: Path, baseline_json: Path | None, *, sync_depth: int = 3
) -> dict:
    out = tmp_path / "report.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        out,
        sync_depth=sync_depth,
        baseline_path=baseline_json,
    )
    return json.loads(out.read_text())


def test_baseline_identical_run_yields_zero_kept(tmp_path: Path) -> None:
    """`--baseline <itself>` on the same fixture moves every finding to
    the carryover tally — kept count is zero, exit code is zero."""
    _strict_skip_if_missing()
    # First run: emit a JSON report to use as the baseline.
    baseline = tmp_path / "baseline.json"
    code = _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        baseline,
        sync_depth=3,
    )
    assert code == 1  # one CDC-002 in the baseline run
    baseline_payload = json.loads(baseline.read_text())
    assert baseline_payload["summary"]["violations"] == 1

    # Second run with the baseline applied: the CDC-002 finding moves
    # from `violations` to `baseline_carryover`.
    out = tmp_path / "second.json"
    code = _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        out,
        sync_depth=3,
        baseline_path=baseline,
    )
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["baseline_carryover"] == 1
    assert payload["baseline_carryover"][0]["rule_id"] == "CDC-002"
    # And the exit code drops to 0 — carryover doesn't fail the build.
    assert code == 0


def test_baseline_empty_keeps_every_finding(tmp_path: Path) -> None:
    """`--baseline <empty-report>` is the same as no baseline — every
    current finding stays in the kept set."""
    _strict_skip_if_missing()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"violations": []}))
    payload = _run_baseline_test(tmp_path, baseline)
    assert payload["summary"]["violations"] == 1
    assert payload["summary"]["baseline_carryover"] == 0


def test_baseline_filters_only_matching_keys(tmp_path: Path) -> None:
    """A baseline that lists a DIFFERENT finding (different rule_id)
    leaves the current finding untouched. The filter is exact-match on
    (rule_id, cell_name, message), not blanket suppression."""
    _strict_skip_if_missing()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "violations": [
                    {
                        "rule_id": "CDC-999",
                        "cell_name": "ghost",
                        "message": "phantom finding",
                    }
                ]
            }
        )
    )
    payload = _run_baseline_test(tmp_path, baseline)
    assert payload["summary"]["violations"] == 1
    assert payload["summary"]["baseline_carryover"] == 0


def test_baseline_sarif_marks_carryover_as_suppressed(tmp_path: Path) -> None:
    """SARIF entries for baseline-carried findings carry a
    ``suppressions`` field so GitHub Code Scanning doesn't fail the
    build on a finding the PR already inherited from main."""
    _strict_skip_if_missing()
    baseline = tmp_path / "baseline.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        baseline,
        sync_depth=3,
    )
    out = tmp_path / "report.sarif"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.sarif,
        out,
        sync_depth=3,
        baseline_path=baseline,
    )
    payload = json.loads(out.read_text())
    results = payload["runs"][0]["results"]
    assert len(results) == 1
    entry = results[0]
    assert entry["ruleId"] == "CDC-002"
    assert "suppressions" in entry
    assert entry["suppressions"][0]["justification"] == "carried over from baseline"


def test_baseline_carryover_chains(tmp_path: Path) -> None:
    """A finding already in the baseline's ``baseline_carryover`` list
    stays carried over on the next run too. Without this, re-baselining
    would re-flag inherited findings."""
    _strict_skip_if_missing()
    # Run once to capture a realistic violation entry, then transplant
    # it into the carryover bucket of a synthesised baseline file. This
    # avoids hard-coding the (rule-generated) message and cell_name.
    real = tmp_path / "real.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        real,
        sync_depth=3,
    )
    real_payload = json.loads(real.read_text())
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "violations": [],
                "baseline_carryover": real_payload["violations"],
            }
        )
    )
    payload = _run_baseline_test(tmp_path, baseline)
    # The current finding matches via the baseline's carryover bucket.
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["baseline_carryover"] == 1


# --- hierarchical instance path (issue #75) ---------------------------------
#
# Phase 1 of #46: every Violation carries an ``instance_path: tuple[str, ...]``
# resolved from its ``cell_name``. The resolver runs in the CLI boundary, not
# in the rule pack. These tests pin the resolver's behaviour against the
# three cell-name shapes the analyzer sees today (Yosys ``$flatten\`` prefix,
# top-level Yosys auto-name, slang dotted) and verify the field actually
# reaches ``AnalysisResult.violations`` on a real fixture.


@pytest.mark.parametrize(
    "cell_name, expected",
    [
        # Top instance / None → empty tuple.
        (None, ()),
        ("$procdff$42", ()),
        ("$logic_and$tests/foo.sv:55$13", ()),
        # Yosys flatten — single-level instance.
        ("$flatten\\u_sync_ack.$procdff$84", ("u_sync_ack",)),
        # Yosys flatten — nested (multi-level). Yosys emits *one*
        # ``$flatten\`` prefix per cell regardless of nesting depth;
        # inner instances are encoded as dot-separated ``\``-escaped
        # identifiers inside the same name.
        ("$flatten\\u_hs_cmd.\\u_sync_ack.$procdff$893", ("u_hs_cmd", "u_sync_ack")),
        # Yosys flatten — leaf cell with embedded source path inside
        # its auto-name (``$add$<file>:<line>$N``). The path contains
        # ``.sv`` which must NOT be tokenized as more hierarchy; the
        # leaf-detection guard on ``$`` prefix protects this.
        (
            "$flatten\\u_afifo.$add$/abs/path/ip_async_fifo.sv:39$314",
            ("u_afifo",),
        ),
        # slang frontend dotted shape; the last component is the leaf
        # symbol, not an instance.
        ("u_b0.q", ("u_b0",)),
        # Pathological — cell names containing dots are not produced by
        # either shipping frontend, but the resolver still does
        # something sensible (treats the last component as the leaf).
        ("u_b0.cell_with.dots", ("u_b0", "cell_with")),
    ],
)
def test_instance_path_resolver_table(
    result: AnalysisResult, cell_name: str | None, expected: tuple[str, ...]
) -> None:
    # The ``module`` argument is currently unused but the contract takes
    # it for parity with ``_source_location``; passing the existing
    # result's module satisfies the type without changing behaviour.
    assert _instance_path(result.module, cell_name) == expected


def test_instance_path_default_on_violation_is_empty_tuple() -> None:
    """A ``Violation`` constructed without ``instance_path`` defaults to
    ``()``. This keeps existing test literals across the suite valid
    without an audit, and matches the resolver's top-instance result."""
    from rtl_buddy_cdc.rules import Violation

    v = Violation(rule_id="CDC-001", severity="error", message="x")
    assert v.instance_path == ()


def test_instance_path_populated_on_handshake_strict_run(tmp_path: Path) -> None:
    """End-to-end: a real fixture with nested instances (ip_cdc_handshake
    instantiates two ``ip_cdc_sync`` children, ``u_sync_ack`` and
    ``u_sync_req``) at ``--sync-depth 3`` produces two CDC-002 findings
    whose ``cell_name`` carries the ``$flatten\\u_sync_*`` prefix. The
    CLI boundary must populate ``instance_path`` accordingly."""
    fix_dir = Path(__file__).parent / "fixtures" / "ip_cdc_handshake"
    json_path = fix_dir / "ip_cdc_handshake.json"
    sdc_path = fix_dir / "ip_cdc_handshake.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    out = tmp_path / "report.json"
    _analyze_and_report(
        json_path,
        sdc_path,
        None,
        OutputFormat.json,
        out,
        sync_depth=3,
    )
    payload = json.loads(out.read_text())
    # Two CDC-002 findings (one per sync chain); each must come from
    # inside one of the named child instances.
    rule_ids = {v["rule_id"] for v in payload["violations"]}
    assert rule_ids == {"CDC-002"}, rule_ids
    # ``instance_path`` is not yet emitted in the JSON payload (phase 2),
    # so rebuild the AnalysisResult to peek at the in-memory field.
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    raw = run_all_rules(module, async_crossings, spec, required_depth=3)
    # Run the same boundary post-pass the CLI runs.
    resolved = [
        dataclasses.replace(v, instance_path=_instance_path(module, v.cell_name))
        for v in raw
    ]
    assert {v.instance_path for v in resolved} == {("u_sync_ack",), ("u_sync_req",)}


# --- JSON per-instance shape (phase 2 of #46) -------------------------------
#
# Two surfaces this phase pins: ``instance_path: list[str]`` on every
# violation / suppressed / baseline_carryover dict (always present, ``[]``
# at top, never null/missing), and a top-level ``by_instance`` aggregation
# of *kept* violations sorted by path.


def test_json_violations_carry_instance_path_field(result: AnalysisResult) -> None:
    """Every entry in ``violations`` has ``instance_path: list[str]``
    populated. The shape is unconditional so downstream consumers
    don't have to handle a missing or null key."""
    buf = io.StringIO()
    render_json(result, buf)
    payload = json.loads(buf.getvalue())
    assert len(payload["violations"]) == 1
    v = payload["violations"][0]
    assert "instance_path" in v
    assert isinstance(v["instance_path"], list)
    # bad_single_ff_sync is a flat fixture — the single CDC-001 lives
    # at top, so the path is empty.
    assert v["instance_path"] == []


def test_json_suppressed_and_carryover_carry_instance_path(tmp_path: Path) -> None:
    """``suppressed`` and ``baseline_carryover`` entries also carry the
    field. The CLI boundary post-pass runs on all three lists, so any
    consumer iterating one of the auxiliary buckets sees a populated
    path too."""
    _strict_skip_if_missing()
    # First, baseline JSON to feed back in.
    baseline = tmp_path / "baseline.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        baseline,
        sync_depth=3,
    )
    waivers = tmp_path / "cdc.waivers"
    # An unrelated waiver so the kept list contains a non-suppressed
    # finding and the suppressed list is empty in this particular setup.
    # We re-purpose the same fixture for the carryover bucket assertion.
    waivers.write_text("waive CDC-999 .* unmatched\n")
    out = tmp_path / "report.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        waivers,
        OutputFormat.json,
        out,
        sync_depth=3,
        baseline_path=baseline,
    )
    payload = json.loads(out.read_text())
    # Every carryover entry has the field. (Suppressed list may be empty
    # in this configuration; the contract is "if present, populated".)
    for entry in payload["baseline_carryover"]:
        assert "instance_path" in entry
        assert isinstance(entry["instance_path"], list)


def test_json_by_instance_summary_present_and_sorted(result: AnalysisResult) -> None:
    """Top-level ``by_instance`` is a sorted list of per-path aggregates.
    Each entry has ``instance_path: list[str]``, a ``violations`` count,
    and a per-rule ``rules`` map."""
    buf = io.StringIO()
    render_json(result, buf)
    payload = json.loads(buf.getvalue())
    assert "by_instance" in payload
    by = payload["by_instance"]
    assert isinstance(by, list)
    # bad_single_ff_sync has one CDC-001 at top.
    assert by == [{"instance_path": [], "violations": 1, "rules": {"CDC-001": 1}}]


def test_json_by_instance_groups_handshake_strict(tmp_path: Path) -> None:
    """End-to-end: at ``--sync-depth 3``, ``ip_cdc_handshake`` fires two
    CDC-002 findings — one inside ``u_sync_ack`` and one inside
    ``u_sync_req``. The ``by_instance`` aggregation bucketizes them as
    two separate entries, sorted lexicographically."""
    fix_dir = Path(__file__).parent / "fixtures" / "ip_cdc_handshake"
    json_path = fix_dir / "ip_cdc_handshake.json"
    sdc_path = fix_dir / "ip_cdc_handshake.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    out = tmp_path / "report.json"
    _analyze_and_report(
        json_path,
        sdc_path,
        None,
        OutputFormat.json,
        out,
        sync_depth=3,
    )
    payload = json.loads(out.read_text())
    assert payload["by_instance"] == [
        {
            "instance_path": ["u_sync_ack"],
            "violations": 1,
            "rules": {"CDC-002": 1},
        },
        {
            "instance_path": ["u_sync_req"],
            "violations": 1,
            "rules": {"CDC-002": 1},
        },
    ]


def test_json_by_instance_excludes_suppressed_and_carryover(tmp_path: Path) -> None:
    """``by_instance`` counts only the *kept* violations. Suppressed
    waivered findings and baseline-carried entries are intentionally
    excluded — the aggregation is a "what's actually failing" view, not
    a structural inventory."""
    _strict_skip_if_missing()
    # First run: emit a baseline JSON to use as the carryover source.
    baseline = tmp_path / "baseline.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        baseline,
        sync_depth=3,
    )
    # Second run with the baseline applied: the single finding moves
    # from kept to baseline_carryover. ``by_instance`` should be empty.
    out = tmp_path / "second.json"
    _analyze_and_report(
        _STRICT_JSON,
        _STRICT_SDC,
        None,
        OutputFormat.json,
        out,
        sync_depth=3,
        baseline_path=baseline,
    )
    payload = json.loads(out.read_text())
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["baseline_carryover"] == 1
    assert payload["by_instance"] == []


def test_json_contract_still_stable_after_phase2(result: AnalysisResult) -> None:
    """Adding ``instance_path`` and ``by_instance`` is purely additive.
    The cross-repo JSON contract (``summary.violations``,
    ``summary.suppressed``, ``summary.crossings``) must still resolve
    to its declared types."""
    buf = io.StringIO()
    render_json(result, buf)
    payload = json.loads(buf.getvalue())
    for dotted, expected_type in JSON_CONTRACT.items():
        value = _walk_dotted(payload, dotted)
        assert isinstance(value, expected_type), dotted


# --- SARIF logicalLocations (phase 3 of #46) --------------------------------
#
# SARIF spec: ``location`` can carry ``physicalLocation`` (file/line/column)
# and/or ``logicalLocations`` (hierarchical / semantic location). The
# analyzer always populates ``physicalLocation`` from the cell's ``src``
# attribute; phase 3 additionally populates ``logicalLocations`` from
# ``instance_path`` when non-empty, omitting the field at top-instance
# (rather than emitting an empty array) so the output diff stays minimal
# on flat fixtures.


def test_sarif_logical_locations_absent_at_top_instance(
    result: AnalysisResult,
) -> None:
    """A top-instance finding emits ``physicalLocation`` only; the
    ``logicalLocations`` field is *absent* (not present-but-empty)."""
    buf = io.StringIO()
    render_sarif(result, buf)
    data = json.loads(buf.getvalue())
    assert len(data["runs"][0]["results"]) == 1
    loc = data["runs"][0]["results"][0]["locations"][0]
    assert "physicalLocation" in loc
    assert "logicalLocations" not in loc


def test_sarif_logical_locations_emitted_for_nested_handshake_strict(
    tmp_path: Path,
) -> None:
    """End-to-end: ``ip_cdc_handshake`` at ``--sync-depth 3`` fires two
    CDC-002 findings inside ``u_sync_ack`` and ``u_sync_req``. Each
    SARIF result must carry a single-entry ``logicalLocations`` with
    ``fullyQualifiedName`` matching the instance path."""
    fix_dir = Path(__file__).parent / "fixtures" / "ip_cdc_handshake"
    json_path = fix_dir / "ip_cdc_handshake.json"
    sdc_path = fix_dir / "ip_cdc_handshake.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    out = tmp_path / "report.sarif"
    _analyze_and_report(
        json_path,
        sdc_path,
        None,
        OutputFormat.sarif,
        out,
        sync_depth=3,
    )
    payload = json.loads(out.read_text())
    results = payload["runs"][0]["results"]
    assert len(results) == 2
    fqns: set[str] = set()
    for r in results:
        # physicalLocation is independent of instance_path; both must be
        # present on a violation that has a source location AND an
        # instance path.
        loc = r["locations"][0]
        assert "physicalLocation" in loc
        assert "logicalLocations" in loc
        ll = loc["logicalLocations"]
        assert len(ll) == 1
        assert ll[0]["kind"] == "module"
        fqns.add(ll[0]["fullyQualifiedName"])
        # ``name`` is the leaf component of the path.
        assert ll[0]["name"] == ll[0]["fullyQualifiedName"].rsplit(".", 1)[-1]
    assert fqns == {"u_sync_ack", "u_sync_req"}


def test_sarif_logical_locations_on_suppressed_entries(
    result: AnalysisResult,
) -> None:
    """Suppressed (waivered) entries go through the same emission path
    as kept findings, so a waivered finding with a non-empty
    ``instance_path`` also picks up ``logicalLocations``. Using a
    synthetic suppressed list so the test doesn't depend on a
    multi-instance bad fixture being available."""
    # Synthesise a suppressed-violation list with one entry that has a
    # populated instance_path, mirroring what the CLI boundary would
    # have produced if the fixture were hierarchical.
    import re

    from rtl_buddy_cdc.rules import Violation as RuleViolation
    from rtl_buddy_cdc.waivers import SuppressedViolation, Waiver

    v_nested = RuleViolation(
        rule_id="CDC-001",
        severity="error",
        message="nested",
        instance_path=("u_block_a", "u_sync"),
    )
    fake_waiver = Waiver(
        rule_pattern="CDC-001",
        regex=re.compile(".*"),
        reason="hand-reviewed",
        source_line=1,
    )
    waived_result = AnalysisResult(
        module=result.module,
        domains=result.domains,
        crossings=result.crossings,
        async_crossings=result.async_crossings,
        spec=result.spec,
        violations=[],
        suppressed=[SuppressedViolation(violation=v_nested, waiver=fake_waiver)],
    )
    buf = io.StringIO()
    render_sarif(waived_result, buf)
    data = json.loads(buf.getvalue())
    # Suppressed entries are emitted as results carrying a SARIF
    # ``suppressions`` field; check ours carries logicalLocations too.
    suppressed_entries = [r for r in data["runs"][0]["results"] if "suppressions" in r]
    assert len(suppressed_entries) == 1
    entry = suppressed_entries[0]
    # No physical location (no real cell), but the logical location is
    # populated from the instance_path.
    locations = entry.get("locations", [])
    assert len(locations) == 1
    ll = locations[0]["logicalLocations"]
    assert ll[0]["fullyQualifiedName"] == "u_block_a.u_sync"
    assert ll[0]["name"] == "u_sync"
    assert ll[0]["kind"] == "module"
