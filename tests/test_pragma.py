"""In-RTL pragma scanner tests (issue #41).

Phase 1 is the scanner alone: it turns ``// rbcdc: disable-rule …``
magic comments into :class:`rtl_buddy_cdc.waivers.Waiver` records.
Nothing is applied to a rule run here — that's phase 2.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy_cdc import pragma


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_scan_line_comment_single_rule(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "dut.sv",
        """module dut(input logic clk, input logic d, output logic q);
  // rbcdc: disable-rule CDC-001
  always_ff @(posedge clk) q <= d;
endmodule
""",
    )
    waivers = pragma.scan([src])
    assert len(waivers) == 1
    w = waivers[0]
    assert w.rule_pattern == "CDC-001"
    assert w.source_line == 2
    assert w.origin == str(src)
    assert w.reason == ""
    # File scope: the compiled regex matches the file's basename.
    assert w.regex.search("/somewhere/else/dut.sv:12.3-12.9")
    assert not w.regex.search("/somewhere/other.sv:1")


def test_scan_multi_rule_expands(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "multi.sv",
        "// rbcdc: disable-rule CDC-001,CDC-002 hand-reviewed handshake\n",
    )
    waivers = pragma.scan([src])
    assert [w.rule_pattern for w in waivers] == ["CDC-001", "CDC-002"]
    assert {w.reason for w in waivers} == {"hand-reviewed handshake"}
    assert {w.source_line for w in waivers} == {1}


def test_scan_multi_rule_tolerates_spaces(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "spaced.sv",
        "//rbcdc: disable-rule CDC-001 , RDC-001 , CDC-BBX  spaced out\n",
    )
    waivers = pragma.scan([src])
    assert [w.rule_pattern for w in waivers] == ["CDC-001", "RDC-001", "CDC-BBX"]
    assert waivers[0].reason == "spaced out"


def test_scan_block_comment_strips_terminator(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "block.sv",
        "logic q;  /* rbcdc: disable-rule CDC-005 library cell */\n",
    )
    (w,) = pragma.scan([src])
    assert w.rule_pattern == "CDC-005"
    assert w.reason == "library cell"


def test_scan_block_comment_without_reason(tmp_path: Path) -> None:
    src = _write(tmp_path, "bare.sv", "/* rbcdc: disable-rule CDC-005 */\n")
    (w,) = pragma.scan([src])
    assert w.reason == ""


def test_scan_wildcard_rule(tmp_path: Path) -> None:
    src = _write(tmp_path, "star.sv", "// rbcdc: disable-rule * generated code\n")
    (w,) = pragma.scan([src])
    assert w.rule_pattern == "*"
    assert w.reason == "generated code"


def test_scan_ignores_unrelated_comments(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "noise.sv",
        """// a plain comment
// rbcdc: enable-rule CDC-001
// rbsch: disable-rule CDC-001
// rtl-buddy-cdc disable-rule CDC-001
(* cdc_sync *) logic q;
disable-rule CDC-001
// rbcdc: disable-rule
""",
    )
    assert pragma.scan([src]) == []


def test_scan_records_every_pragma_across_files(tmp_path: Path) -> None:
    a = _write(
        tmp_path,
        "a.sv",
        "// rbcdc: disable-rule CDC-001 first\n\n// rbcdc: disable-rule CDC-004\n",
    )
    b = _write(tmp_path, "b.sv", "// rbcdc: disable-rule CDC-013 second\n")
    waivers = pragma.scan([a, b])
    assert [
        (w.rule_pattern, w.source_line, Path(w.origin or "").name) for w in waivers
    ] == [
        ("CDC-001", 1, "a.sv"),
        ("CDC-004", 3, "a.sv"),
        ("CDC-013", 1, "b.sv"),
    ]


def test_scan_text_is_path_attributed() -> None:
    """``scan_text`` takes already-read text; the path is only used for
    the file-scope regex and the ``origin`` label."""
    (w,) = pragma.scan_text("// rbcdc: disable-rule CDC-001\n", Path("rtl/top.sv"))
    assert w.origin == "rtl/top.sv"
    assert w.regex.pattern == r"top\.sv"


def test_scan_tolerates_undecodable_bytes(tmp_path: Path) -> None:
    src = tmp_path / "latin.sv"
    src.write_bytes(b"// caf\xe9\n// rbcdc: disable-rule CDC-001 ok\n")
    (w,) = pragma.scan([src])
    assert w.rule_pattern == "CDC-001"
    assert w.source_line == 2


# --- block scoping (issue #43) ----------------------------------------------
#
# A `disable-rule` opens a region an `enable-rule` for the same rule id
# closes, half-open on [disable_line, enable_line).


def test_block_scope_pairs_disable_with_enable(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "block.sv",
        """logic a;
// rbcdc: disable-rule CDC-001 vetted by hand
always_ff @(posedge clk) a <= d;
// rbcdc: enable-rule CDC-001
logic b;
""",
    )
    (w,) = pragma.scan([src])
    assert (w.start_line, w.end_line) == (2, 4)
    assert w.source_line == 2  # the disable line, for the report
    assert w.reason == "vetted by hand"


def test_block_without_enable_runs_to_end_of_file(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "eof.sv",
        "logic a;\n// rbcdc: disable-rule CDC-001\nlogic b;\nlogic c;\n",
    )
    (w,) = pragma.scan([src])
    assert (w.start_line, w.end_line) == (2, None)


def test_interleaved_blocks_for_different_rules_are_independent(
    tmp_path: Path,
) -> None:
    src = _write(
        tmp_path,
        "nested.sv",
        """// rbcdc: disable-rule CDC-001 outer
// rbcdc: disable-rule CDC-004 inner
logic a;
// rbcdc: enable-rule CDC-004
logic b;
// rbcdc: enable-rule CDC-001
""",
    )
    scopes = {
        (w.rule_pattern, w.start_line, w.end_line, w.reason) for w in pragma.scan([src])
    }
    assert scopes == {
        ("CDC-001", 1, 6, "outer"),
        ("CDC-004", 2, 4, "inner"),
    }


def test_multi_rule_block_closes_per_rule(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "multiblock.sv",
        """// rbcdc: disable-rule CDC-001,CDC-002 both
logic a;
// rbcdc: enable-rule CDC-002
logic b;
""",
    )
    scopes = {(w.rule_pattern, w.start_line, w.end_line) for w in pragma.scan([src])}
    assert scopes == {("CDC-001", 1, None), ("CDC-002", 1, 3)}


def test_redisabling_an_open_rule_starts_a_fresh_region(tmp_path: Path) -> None:
    """The running region ends where the new one begins, so the newer
    reason applies from there and the two don't overlap."""
    src = _write(
        tmp_path,
        "redisable.sv",
        """// rbcdc: disable-rule CDC-001 first reason
logic a;
// rbcdc: disable-rule CDC-001 second reason
logic b;
""",
    )
    first, second = pragma.scan([src])
    assert (first.start_line, first.end_line, first.reason) == (1, 3, "first reason")
    assert (second.start_line, second.end_line, second.reason) == (
        3,
        None,
        "second reason",
    )


def test_stray_enable_rule_is_ignored(tmp_path: Path) -> None:
    src = _write(tmp_path, "stray.sv", "// rbcdc: enable-rule CDC-001\nlogic a;\n")
    assert pragma.scan([src]) == []


def test_regions_are_emitted_in_disable_line_order(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "order.sv",
        """// rbcdc: disable-rule CDC-001
// rbcdc: disable-rule CDC-004
// rbcdc: enable-rule CDC-004
// rbcdc: enable-rule CDC-001
""",
    )
    assert [w.start_line for w in pragma.scan([src])] == [1, 2]
