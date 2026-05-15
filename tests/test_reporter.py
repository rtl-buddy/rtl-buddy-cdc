"""Reporter unit tests — text/json/sarif rendering of an AnalysisResult."""

from __future__ import annotations

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
