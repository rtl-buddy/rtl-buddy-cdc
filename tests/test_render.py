"""Tests for ``rtl_buddy_cdc.render`` and the ``render`` CLI subcommand.

Covers schema validation, deterministic output, the two fixtures
called out in issue #162's sketches, and CLI wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.render import RenderError, render_mermaid

FIXTURES = Path(__file__).parent / "fixtures"
HANDSHAKE_MAP = FIXTURES / "ip_cdc_handshake" / "ip_cdc_handshake.domain-map.json"


# --- pure-function tests ----------------------------------------------------


def test_render_mermaid_is_deterministic() -> None:
    """Same input -> identical bytes. The renderer sorts every emitted
    collection (see issue #162 "Rules locked in")."""
    map_data = json.loads(HANDSHAKE_MAP.read_text())
    assert render_mermaid(map_data) == render_mermaid(map_data)


def test_render_mermaid_emits_fenced_block_and_clock_subgraphs() -> None:
    map_data = json.loads(HANDSHAKE_MAP.read_text())
    out = render_mermaid(map_data)

    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")
    assert "flowchart LR" in out

    # One subgraph per clock. The handshake fixture has src_clk + dst_clk.
    assert 'subgraph clk_src_clk["src_clk · 10.0 ns"]' in out
    assert 'subgraph clk_dst_clk["dst_clk · 7.5 ns"]' in out


def test_render_mermaid_uses_dashed_edge_for_async_crossing() -> None:
    map_data = json.loads(HANDSHAKE_MAP.read_text())
    out = render_mermaid(map_data)

    # All three handshake crossings are async-per-SDC; each must surface
    # the warning marker. Exclude the legend's demonstrator async edge
    # — it has the same ``⚠ async`` motif but connects placeholder
    # ``lg_async_*`` ids, not real crossing endpoints.
    async_edges = [
        line
        for line in out.splitlines()
        if "⚠ async" in line and "lg_async" not in line
    ]
    assert len(async_edges) == len(map_data["crossings"])
    # Edge syntax — dashed, with width annotation.
    for line in async_edges:
        assert ".->" in line and "b" in line


def test_render_mermaid_skips_warning_for_synchronous_crossings() -> None:
    """When a crossing is listed but ``async_per_sdc`` is false (a
    false-path or same-group pair) we draw a plain link, not a warning
    edge — per issue #162 rules."""
    map_data = {
        "schema_version": "1.0",
        "design": {"top": "t"},
        "clocks": [
            {"name": "ca", "period": 10.0},
            {"name": "cb", "period": 10.0},
        ],
        "generated_clocks": [],
        "flop_domains": [
            {"instance_path": "t.fa", "clock": "ca"},
            {"instance_path": "t.fb", "clock": "cb"},
        ],
        "port_domains": [],
        "crossings": [
            {
                "src_clock": "ca",
                "dst_clock": "cb",
                "src_flop": "t.fa",
                "dst_flop": "t.fb",
                "width": 1,
                "async_per_sdc": False,
            }
        ],
    }
    out = render_mermaid(map_data)
    # The sync crossing must not be tagged with the ⚠ async marker.
    # Exclude the legend's demonstrator edge (placeholder ``lg_async_*``
    # ids carry the motif but aren't real crossings) from the search.
    crossing_lines = [line for line in out.splitlines() if "lg_async" not in line]
    assert all("⚠ async" not in line for line in crossing_lines)
    assert " --- " in out


def test_render_mermaid_strips_top_module_prefix() -> None:
    """Flop labels read better when the redundant top-module prefix is
    stripped. ``ip_cdc_handshake.u_sync_req.$procdff$84`` shows as
    ``u_sync_req.$procdff$84``."""
    map_data = json.loads(HANDSHAKE_MAP.read_text())
    out = render_mermaid(map_data)
    assert "u_sync_req.$procdff$84" in out
    # The fully-qualified form must NOT appear in any label.
    assert "ip_cdc_handshake.u_sync_req.$procdff$84" not in out


def test_render_mermaid_emits_port_anchors() -> None:
    map_data = json.loads(HANDSHAKE_MAP.read_text())
    out = render_mermaid(map_data)
    # The handshake fixture has src_data + src_valid input ports.
    assert "src_data⟨in⟩" in out
    assert "src_valid⟨in⟩" in out


def test_render_rejects_missing_schema_version() -> None:
    with pytest.raises(RenderError, match="schema_version"):
        render_mermaid({})


def test_render_rejects_unsupported_schema_major() -> None:
    with pytest.raises(RenderError, match="unsupported"):
        render_mermaid({"schema_version": "2.0"})


def test_render_accepts_minor_version_drift() -> None:
    """Per issue #162, the renderer tolerates new optional fields and
    only checks the schema major. A future ``1.1`` map must still
    render."""
    out = render_mermaid(
        {
            "schema_version": "1.99",
            "design": {"top": "t"},
            "clocks": [],
            "generated_clocks": [],
            "flop_domains": [],
            "port_domains": [],
            "crossings": [],
        }
    )
    assert "flowchart" in out


def test_render_draws_clock_network_crossing_edges() -> None:
    """rtl-buddy-cdc#168: ``clock_network_crossings[]`` (schema 1.1+)
    is drawn with a thick arrow + ⚡ clk-ctrl label so the CDC-010
    flop→flop relationship is visible alongside the data crossings."""
    out = render_mermaid(
        {
            "schema_version": "1.1",
            "design": {"top": "t"},
            "clocks": [
                {"name": "ck0", "period": 10.0},
                {"name": "ck1", "period": 13.3},
            ],
            "generated_clocks": [],
            "flop_domains": [
                {"instance_path": "t.fa", "clock": "ck1"},
                {"instance_path": "t.fb", "clock": "ck0"},
            ],
            "port_domains": [],
            "crossings": [],
            "clock_network_crossings": [
                {
                    "src_clock": "ck1",
                    "dst_clock": "ck0",
                    "src_flop": "t.fa",
                    "dst_flop": "t.fb",
                    "control_cell": "$mux$12",
                    "control_cell_type": "$mux",
                    "control_pin": "S",
                    "control_kind": "mux-select",
                    "async_per_sdc": True,
                }
            ],
        }
    )
    assert "==>" in out, "expected a thick arrow for the clock-network crossing"
    assert "⚡ clk-ctrl (mux S)" in out


def test_render_omits_clock_network_section_when_field_absent() -> None:
    """v1.0 maps don't carry the field; the renderer must treat it as
    an empty list rather than raising."""
    out = render_mermaid(
        {
            "schema_version": "1.0",
            "design": {"top": "t"},
            "clocks": [],
            "generated_clocks": [],
            "flop_domains": [],
            "port_domains": [],
            "crossings": [],
        }
    )
    assert "==>" not in out
    assert "clk-ctrl" not in out


def test_render_surfaces_undeclared_clocks() -> None:
    """If a flop references a clock that's not in ``clocks`` or
    ``generated_clocks`` (e.g. malformed map), we still draw it under
    an ``(undeclared)`` subgraph so the user can see the analyzer
    found something off-spec."""
    out = render_mermaid(
        {
            "schema_version": "1.0",
            "design": {"top": "t"},
            "clocks": [],
            "generated_clocks": [],
            "flop_domains": [
                {"instance_path": "t.f0", "clock": "mystery_clk"},
            ],
            "port_domains": [],
            "crossings": [],
        }
    )
    assert "mystery_clk (undeclared)" in out


# --- CLI tests --------------------------------------------------------------


runner = CliRunner()


def test_render_cli_to_stdout() -> None:
    result = runner.invoke(
        app, ["render", "--map", str(HANDSHAKE_MAP), "--format", "mermaid"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("```mermaid\n")
    assert "ip_cdc_handshake" in result.stdout


def test_render_cli_writes_output_file(tmp_path: Path) -> None:
    out_file = tmp_path / "diagram.md"
    result = runner.invoke(
        app,
        [
            "render",
            "--map",
            str(HANDSHAKE_MAP),
            "--output",
            str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output
    body = out_file.read_text()
    assert body.startswith("```mermaid\n")
    assert "flowchart LR" in body


def test_render_cli_rejects_unknown_schema(tmp_path: Path) -> None:
    bad_map = tmp_path / "bad.json"
    bad_map.write_text(json.dumps({"schema_version": "9.0"}))
    result = runner.invoke(app, ["render", "--map", str(bad_map)])
    assert result.exit_code == 1
    assert "unsupported" in result.output


def test_render_cli_rejects_invalid_json(tmp_path: Path) -> None:
    bad_map = tmp_path / "not-json.json"
    bad_map.write_text("not json at all")
    result = runner.invoke(app, ["render", "--map", str(bad_map)])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output
