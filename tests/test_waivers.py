"""Waiver parser and application tests, plus an end-to-end CLI run."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod, waivers as waivers_mod
from rtl_buddy_cdc.cli import _analyze_and_report, OutputFormat  # noqa: PLC2701
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import Violation, run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_single_ff_sync"
JSON_PATH = FIX_DIR / "bad_single_ff_sync.json"
SDC_PATH = FIX_DIR / "bad_single_ff_sync.sdc"


def test_parse_basic() -> None:
    waivers = waivers_mod.parse(
        """
        # comment line
        waive CDC-001 .*procdff\\$9.* hand-reviewed by jsmith
        waive *       .*generated.*   tool-emitted
        """
    )
    assert len(waivers) == 2
    assert waivers[0].rule_pattern == "CDC-001"
    assert waivers[0].reason.startswith("hand-reviewed")
    assert waivers[1].rule_pattern == "*"


def test_parse_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="line 1"):
        waivers_mod.parse("notavalidline foo bar")


def test_parse_rejects_bad_regex() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        waivers_mod.parse("waive CDC-001 *bogus reason")


def test_apply_suppresses_matching() -> None:
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
    assert len(violations) == 1  # baseline: one CDC-001

    waivers = waivers_mod.parse("waive CDC-001 procdff\\$9 reviewed by team\n")
    kept, suppressed = waivers_mod.apply(violations, waivers)
    assert kept == []
    assert len(suppressed) == 1
    assert suppressed[0].waiver.reason == "reviewed by team"


def test_legacy_cdc_007_alias_suppresses_rdc_001() -> None:
    """Back-compat: a waiver written against the legacy ``CDC-007``
    rule_id continues to suppress the renamed ``RDC-001`` rule.

    Added in #107 when the async-reset-crossing rule was renamed from
    CDC-007 to RDC-001 to join the new RDC (Reset Domain Crossing)
    family. Without the alias path in
    :func:`rtl_buddy_cdc.waivers._rule_pattern_matches`, every
    existing project waiver would silently break — this test guards
    against accidental removal of the alias map."""
    fix_dir = Path(__file__).parent / "fixtures" / "bad_reset_crossing"
    json_path = fix_dir / "bad_reset_crossing.json"
    sdc_path = fix_dir / "bad_reset_crossing.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
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
    rdc = [v for v in violations if v.rule_id == "RDC-001"]
    assert len(rdc) == 1, f"expected one RDC-001, got {violations}"

    # Legacy alias suppresses.
    legacy = waivers_mod.parse("waive CDC-007 .* legacy waiver\n")
    kept, suppressed = waivers_mod.apply(violations, legacy)
    assert kept == [] and len(suppressed) == 1
    assert suppressed[0].waiver.rule_pattern == "CDC-007"
    assert suppressed[0].violation.rule_id == "RDC-001"

    # New canonical id also suppresses (sanity check).
    canonical = waivers_mod.parse("waive RDC-001 .* canonical\n")
    kept2, suppressed2 = waivers_mod.apply(violations, canonical)
    assert kept2 == [] and len(suppressed2) == 1


def test_apply_non_matching_keeps_violation() -> None:
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

    # Wrong rule id, even though regex matches.
    waivers = waivers_mod.parse("waive CDC-005 .* not the right rule\n")
    kept, suppressed = waivers_mod.apply(violations, waivers)
    assert len(kept) == 1
    assert suppressed == []


def test_cli_waiver_drops_exit_code(tmp_path: Path) -> None:
    """End-to-end: a waiver matching the only violation should drive
    the analyzer exit code back to 0."""
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    waiver_file = tmp_path / "cdc.waivers"
    waiver_file.write_text("waive CDC-001 procdff\\$9 reviewed\n")
    out = tmp_path / "report.txt"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, waiver_file, OutputFormat.text, out)
    assert code == 0
    text = out.read_text()
    assert "Suppressed by waivers" in text
    assert "reviewed" in text


def test_cli_waiver_drops_exit_code_json(tmp_path: Path) -> None:
    """End-to-end with ``--format json`` (issue #14, gap 1).

    The text-format equivalent above doesn't exercise the JSON dispatch
    branch in ``cli._analyze_module_and_report`` or the JSON reporter's
    ``suppressed[]`` array — a regression that swapped the json/text
    dispatch or dropped the ``waiver`` payload would slip through with
    only the text test in place.
    """
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    import json as _json

    waiver_file = tmp_path / "cdc.waivers"
    waiver_file.write_text("waive CDC-001 procdff\\$9 reviewed\n")
    out = tmp_path / "report.json"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, waiver_file, OutputFormat.json, out)
    assert code == 0  # fully waived → exit 0 same as text path
    payload = _json.loads(out.read_text())
    assert payload["summary"]["violations"] == 0
    assert payload["summary"]["suppressed"] == 1
    assert len(payload["suppressed"]) == 1
    assert payload["suppressed"][0]["rule_id"] == "CDC-001"
    assert payload["suppressed"][0]["waiver"]["reason"] == "reviewed"


def test_cli_waiver_drops_exit_code_sarif(tmp_path: Path) -> None:
    """End-to-end with ``--format sarif`` (issue #14, gap 1).

    SARIF consumers (GitHub Code Scanning, etc.) key off the
    ``suppressions`` field to decide whether an alert fails the
    build. This test pins the CLI plumbing: a waiver matching the only
    violation should leave the alert in the SARIF output but flag it as
    suppressed, and drive the exit code to 0.
    """
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    import json as _json

    waiver_file = tmp_path / "cdc.waivers"
    waiver_file.write_text("waive CDC-001 procdff\\$9 reviewed by team\n")
    out = tmp_path / "report.sarif"
    code = _analyze_and_report(
        JSON_PATH, SDC_PATH, waiver_file, OutputFormat.sarif, out
    )
    assert code == 0
    data = _json.loads(out.read_text())
    results = data["runs"][0]["results"]
    assert len(results) == 1
    entry = results[0]
    assert entry["ruleId"] == "CDC-001"
    assert "suppressions" in entry
    assert entry["suppressions"][0]["kind"] == "external"
    assert entry["suppressions"][0]["status"] == "accepted"
    assert entry["suppressions"][0]["justification"] == "reviewed by team"


def test_cli_no_waiver_still_fails(tmp_path: Path) -> None:
    if not JSON_PATH.exists():
        pytest.skip(f"fixture not built: {JSON_PATH}")
    out = tmp_path / "report.txt"
    code = _analyze_and_report(JSON_PATH, SDC_PATH, None, OutputFormat.text, out)
    assert code == 1


# --- pragma-origin waivers (issue #42) --------------------------------------
#
# A waiver carrying an ``origin`` came from an in-RTL ``// rbcdc:``
# pragma. Its scope is the file it was written in, so ``apply`` matches
# it against the violation's source location and nothing else — never
# the cell name or the message, which is what an ordinary waiver-file
# regex targets.


def _pragma_waiver(rule: str = "CDC-001", file: str = "dut.sv") -> waivers_mod.Waiver:
    return waivers_mod.Waiver(
        rule_pattern=rule,
        regex=re.compile(re.escape(file)),
        reason="in-RTL pragma",
        source_line=12,
        origin=f"rtl/{file}",
    )


def _violation(rule: str = "CDC-001", cell: str = "$procdff$9") -> Violation:
    return Violation(
        rule_id=rule,
        severity="error",
        message="unsynchronized control crossing",
        cell_name=cell,
    )


def test_pragma_waiver_matches_on_the_source_file() -> None:
    v = _violation()
    kept, suppressed = waivers_mod.apply(
        [v],
        [_pragma_waiver()],
        locate=lambda _v: waivers_mod.SourceRef("rtl/dut.sv"),
    )
    assert kept == []
    assert suppressed[0].waiver.origin == "rtl/dut.sv"


def test_pragma_waiver_ignores_a_different_file() -> None:
    kept, suppressed = waivers_mod.apply(
        [_violation()],
        [_pragma_waiver()],
        locate=lambda _v: waivers_mod.SourceRef("rtl/other.sv"),
    )
    assert suppressed == []
    assert len(kept) == 1


def test_pragma_waiver_needs_a_resolvable_location() -> None:
    """No location, no file-scoped suppression — the finding stands
    rather than being waived on a guess. Covers both an absent
    resolver (the ``analyze`` path) and an unlocatable cell."""
    for resolver in (None, lambda _v: None):
        kept, suppressed = waivers_mod.apply(
            [_violation()], [_pragma_waiver()], locate=resolver
        )
        assert suppressed == []
        assert len(kept) == 1


def test_pragma_waiver_never_matches_the_cell_name_or_message() -> None:
    """The file-scope regex is not a name pattern: a cell that happens
    to contain the basename doesn't get waived when its own source file
    is elsewhere."""
    v = _violation(cell="u_dut.sv_wrapper")
    kept, suppressed = waivers_mod.apply(
        [v],
        [_pragma_waiver()],
        locate=lambda _v: waivers_mod.SourceRef("rtl/elsewhere.sv"),
    )
    assert suppressed == []
    assert len(kept) == 1


def test_pragma_waiver_still_honours_the_rule_id() -> None:
    kept, suppressed = waivers_mod.apply(
        [_violation(rule="CDC-004")],
        [_pragma_waiver(rule="CDC-001")],
        locate=lambda _v: waivers_mod.SourceRef("rtl/dut.sv"),
    )
    assert suppressed == []
    assert len(kept) == 1


def _block_waiver(start: int, end: int | None) -> waivers_mod.Waiver:
    return waivers_mod.Waiver(
        rule_pattern="CDC-001",
        regex=re.compile(re.escape("dut.sv")),
        reason="block-scoped pragma",
        source_line=start,
        origin="rtl/dut.sv",
        start_line=start,
        end_line=end,
    )


def _at(line: int | None):
    return lambda _v: waivers_mod.SourceRef("rtl/dut.sv", line)


def test_block_scope_is_half_open() -> None:
    """[start, end): the disable line is inside, the enable line is not."""
    w = _block_waiver(10, 20)
    inside = [9, 10, 19, 20, 21]
    suppressed_at = []
    for line in inside:
        kept, sup = waivers_mod.apply([_violation()], [w], locate=_at(line))
        suppressed_at.append(bool(sup) and not kept)
    assert suppressed_at == [False, True, True, False, False]


def test_open_block_suppresses_to_end_of_file() -> None:
    w = _block_waiver(10, None)
    _, before = waivers_mod.apply([_violation()], [w], locate=_at(9))
    _, after = waivers_mod.apply([_violation()], [w], locate=_at(9_999))
    assert before == []
    assert len(after) == 1


def test_block_scope_falls_back_to_file_scope_without_a_line() -> None:
    """A location that names a file but no line can't be range-checked;
    the pragma degrades to file scope rather than doing nothing."""
    kept, suppressed = waivers_mod.apply(
        [_violation()], [_block_waiver(10, 20)], locate=_at(None)
    )
    assert kept == []
    assert len(suppressed) == 1
