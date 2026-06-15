"""Coverage-focused tests for ``reporter.py`` and ``render.py``.

These exercise rendering branches the canonical ``test_reporter.py`` /
``test_render.py`` suites leave uncovered:

* reporter: the ``spec is None`` SKIP path, the ``_Style.for_stream``
  tri-state (``NO_COLOR`` / non-tty / forced-on), the verbose
  per-crossing listing, the ``info`` severity tally, the baseline
  carryover text block, ``src_port`` in the JSON crossing dict, and the
  ``_source_location`` guard branches (unknown cell / no ``src`` attr /
  unparsable ``src``).
* render: ports parked outside every cluster, the orphan-clock
  subgraph + legend label, the no-period legend label, port-sourced and
  malformed crossings, malformed clock-network crossings, the
  ``_clock_header`` no-period path, and ``_safe_id`` escaping.

Everything builds ``AnalysisResult`` / ``Violation`` objects directly
or from a committed Yosys-JSON fixture (loaded with ``netlist.load`` —
no compiled toolchain binary needed), plus synthetic v1.0 domain-map
dicts for the renderer.  No frontend, no subprocess, no network.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.render import (
    RenderError,
    _clock_header,
    _safe_id,
    render_mermaid,
)
from rtl_buddy_cdc.reporter import (
    AnalysisResult,
    _source_location,
    render_json,
    render_sarif,
    render_text,
)
from rtl_buddy_cdc.rules import Violation
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"

_BAD_FIX = FIX_ROOT / "bad_single_ff_sync"
_BAD_JSON = _BAD_FIX / "bad_single_ff_sync.json"
_BAD_SDC = _BAD_FIX / "bad_single_ff_sync.sdc"

_PORT_FIX = FIX_ROOT / "good_port_typed_sync"
_PORT_JSON = _PORT_FIX / "good_port_typed_sync.json"
_PORT_SDC = _PORT_FIX / "good_port_typed_sync.sdc"


def _build_result(json_path: Path, sdc_path: Path | None) -> AnalysisResult:
    """Load a committed fixture and assemble a populated AnalysisResult.

    Passing ``sdc_path=None`` yields a spec-less result so the SKIP
    rendering path can be exercised without inventing a fake spec.
    """
    module = netlist.load(json_path)
    if sdc_path is None:
        return AnalysisResult(
            module=module,
            domains=assign_domains(module),
            crossings=find_crossings(module),
            async_crossings=[],
            spec=None,
            violations=[],
        )
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
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
    violations = run_all_rules(module, async_crossings, spec)
    return AnalysisResult(
        module=module,
        domains=assign_domains(module),
        crossings=crossings,
        async_crossings=async_crossings,
        spec=spec,
        violations=list(violations),
    )


@pytest.fixture(scope="module")
def bad_result() -> AnalysisResult:
    if not _BAD_JSON.exists():
        pytest.skip(f"fixture not built: {_BAD_JSON}")
    return _build_result(_BAD_JSON, _BAD_SDC)


@pytest.fixture(scope="module")
def port_result() -> AnalysisResult:
    if not _PORT_JSON.exists():
        pytest.skip(f"fixture not built: {_PORT_JSON}")
    return _build_result(_PORT_JSON, _PORT_SDC)


# --- reporter: text SKIP path (spec is None) --------------------------------


def test_text_skip_banner_when_no_spec() -> None:
    """With ``spec=None`` the header verdict is ``SKIP`` and the body is
    a single yellow-free (color off) advisory; rule checks never run so
    no Violations block is emitted."""
    if not _BAD_JSON.exists():
        pytest.skip(f"fixture not built: {_BAD_JSON}")
    result = _build_result(_BAD_JSON, None)
    buf = io.StringIO()
    render_text(result, buf, color=False)
    text = buf.getvalue()
    assert "SKIP" in text
    collapsed = " ".join(text.split())
    assert "No SDC supplied" in collapsed
    assert "rule checks are skipped" in collapsed
    # The SKIP path returns before rendering any Violations section.
    assert "Violations" not in text
    assert "No rule violations." not in text


# --- reporter: _Style.for_stream tri-state ----------------------------------


def test_style_no_color_env_disables_ansi(
    bad_result: AnalysisResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``NO_COLOR`` set (any value) forces plain text even on a TTY-like
    stream — the no-color.org convention. No ANSI escape bytes leak."""
    monkeypatch.setenv("NO_COLOR", "1")

    class _FakeTTY(io.StringIO):
        def isatty(self) -> bool:  # pragma: no cover - trivial
            return True

    buf = _FakeTTY()
    # color=None -> the for_stream tri-state consults NO_COLOR + isatty.
    render_text(bad_result, buf, color=None)
    text = buf.getvalue()
    assert "\033[" not in text  # no ANSI escapes
    assert "FAIL" in text


def test_style_non_tty_stream_disables_ansi(
    bad_result: AnalysisResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-TTY stream (default ``io.StringIO`` reports ``isatty``
    False) yields plain text when ``color`` is left at its ``None``
    default."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()  # isatty() -> False
    render_text(bad_result, buf, color=None)
    assert "\033[" not in buf.getvalue()


def test_style_forced_color_emits_ansi(
    bad_result: AnalysisResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``color=True`` overrides the stream/env heuristic and emits ANSI
    even into a plain StringIO. The FAIL verdict is wrapped in the red
    + bold sequences."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()
    render_text(bad_result, buf, color=True)
    text = buf.getvalue()
    assert "\033[31m" in text  # red (FAIL verdict)
    assert "\033[1m" in text  # bold


# --- reporter: verbose per-crossing listing ---------------------------------


def test_text_verbose_lists_each_crossing(port_result: AnalysisResult) -> None:
    """``verbose=True`` appends a ``Crossings:`` section with one line
    per structural crossing. ``good_port_typed_sync`` has a single
    port-sourced crossing, so the line carries the ``(port-sourced)``
    tag and the ``src_clock → dst_clock`` arrow."""
    assert port_result.crossings, "fixture should have at least one crossing"
    buf = io.StringIO()
    render_text(port_result, buf, verbose=True, color=False)
    text = buf.getvalue()
    assert "Crossings:" in text
    c = port_result.crossings[0]
    assert f"{c.src_clock} → {c.dst_clock}" in text
    # The single crossing is port-sourced (src is a typed input port).
    assert "(port-sourced)" in text
    assert "port d_in" in text  # Crossing.src_name for a port source
    assert f"width={c.width}" in text


# --- reporter: info severity tally + baseline carryover text ----------------


def test_text_info_severity_counted_in_summary(bad_result: AnalysisResult) -> None:
    """An ``info`` violation is tallied in the cyan summary segment.
    Built synthetically (no shipping rule emits ``info`` today) so the
    ``counts['info']`` branch in ``_render_violations`` is exercised."""
    info_v = Violation(
        rule_id="CDC-099",
        severity="info",
        message="informational only — nothing actionable",
    )
    result = AnalysisResult(
        module=bad_result.module,
        domains=bad_result.domains,
        crossings=bad_result.crossings,
        async_crossings=bad_result.async_crossings,
        spec=bad_result.spec,
        violations=[info_v],
    )
    buf = io.StringIO()
    render_text(result, buf, color=False)
    text = buf.getvalue()
    assert "1 info" in text
    collapsed = " ".join(text.split())
    assert "informational only" in collapsed


def test_text_renders_baseline_carryover_block(bad_result: AnalysisResult) -> None:
    """A ``baseline_carryover`` entry renders under a dedicated
    ``Carried over from baseline`` header with the rule id and the
    first line of its message — and never inflates the FAIL verdict
    (carryover doesn't drive a verdict change here because there's also
    a kept violation)."""
    carried = Violation(
        rule_id="CDC-002",
        severity="warning",
        message="insufficient synchronizer depth\nsecond detail line",
    )
    result = AnalysisResult(
        module=bad_result.module,
        domains=bad_result.domains,
        crossings=bad_result.crossings,
        async_crossings=bad_result.async_crossings,
        spec=bad_result.spec,
        violations=list(bad_result.violations),
        baseline_carryover=[carried],
    )
    buf = io.StringIO()
    render_text(result, buf, color=False)
    text = buf.getvalue()
    assert "Carried over from baseline (1)" in text
    assert "CDC-002" in text
    # Only the first line of the multi-line message is shown.
    assert "insufficient synchronizer depth" in text
    assert "second detail line" not in text


# --- reporter: JSON src_port + crossing dict --------------------------------


def test_json_crossing_carries_src_port(port_result: AnalysisResult) -> None:
    """A port-sourced crossing serialises with a ``src_port`` key and no
    ``src_flop`` key (the ``_crossing_to_dict`` branch keyed on
    ``c.src_port is not None``)."""
    buf = io.StringIO()
    render_json(port_result, buf)
    payload = json.loads(buf.getvalue())
    assert payload["crossings"], "expected at least one crossing in the payload"
    port_crossings = [c for c in payload["crossings"] if "src_port" in c]
    assert len(port_crossings) == 1
    pc = port_crossings[0]
    assert pc["src_port"] == "d_in"
    assert "src_flop" not in pc  # exactly one source endpoint is set
    assert pc["dst_flop"]  # destination flop is always present
    assert pc["width"] == 1


def test_json_crossing_carries_dst_boundary() -> None:
    """A crossing INTO an abstracted boundary serialises with a
    ``dst_boundary`` ``{instance, port}`` block (#257 virtual-sink seeding
    — the ``_crossing_to_dict`` branch keyed on ``c.dst_boundary``)."""
    from rtl_buddy_cdc.domain import Crossing
    from rtl_buddy_cdc.flops import Flop
    from rtl_buddy_cdc.netlist import Cell
    from rtl_buddy_cdc.reporter import _crossing_to_dict

    sink = Flop(
        cell=Cell(name="u_sub.d_in", type="$boundary_sink", connections={"D": (7,)}),
        clk="<boundary-sink-clk>",
        d=(7,),
        q=(),
    )
    c = Crossing(
        src_clock="clk_b",
        dst_flop=sink,
        dst_clock="clk_a",
        min_hops=0,
        width=4,
        src_flop=None,
        dst_boundary=("u_sub", "d_in"),
    )
    out = _crossing_to_dict(c)
    assert out["dst_boundary"] == {"instance": "u_sub", "port": "d_in"}
    assert out["dst_flop"] == "u_sub.d_in"
    assert out["src_clock"] == "clk_b"
    assert out["dst_clock"] == "clk_a"


# --- reporter: _source_location guard branches ------------------------------


def test_source_location_none_cell_name(bad_result: AnalysisResult) -> None:
    """A ``None`` cell name short-circuits to ``None`` (no anchor cell
    means no source location)."""
    assert _source_location(bad_result.module, None) is None


def test_source_location_unknown_cell(bad_result: AnalysisResult) -> None:
    """A cell name absent from the module resolves to ``None`` rather
    than raising a KeyError."""
    assert _source_location(bad_result.module, "no_such_cell$999") is None


def test_source_location_cell_without_src_attr() -> None:
    """A cell that exists but carries no ``src`` attribute yields
    ``None`` — there's nothing to point at. Drop the ``src`` key off a
    real cell so the ``if not src`` guard runs deterministically."""
    if not _BAD_JSON.exists():
        pytest.skip(f"fixture not built: {_BAD_JSON}")
    module = netlist.load(_BAD_JSON)
    cell_name = next(iter(module.cells))
    attrs = {k: v for k, v in module.cells[cell_name].attributes.items() if k != "src"}
    object.__setattr__(module.cells[cell_name], "attributes", attrs)
    assert _source_location(module, cell_name) is None


def test_source_location_unparsable_src_returns_file_only() -> None:
    """When the ``src`` attribute doesn't match the
    ``file:line.col-line.col`` grammar, the location degrades to a
    bare ``{'file': <raw>}`` with no line/column fields."""
    module = netlist.load(_BAD_JSON) if _BAD_JSON.exists() else None
    if module is None:
        pytest.skip(f"fixture not built: {_BAD_JSON}")
    # Pick any real cell and overwrite its src with a non-conforming
    # value so the regex misses and the fallback branch runs.
    cell_name = next(iter(module.cells))
    object.__setattr__(
        module.cells[cell_name],
        "attributes",
        {**module.cells[cell_name].attributes, "src": "weird_source_no_line_info"},
    )
    loc = _source_location(module, cell_name)
    assert loc == {"file": "weird_source_no_line_info"}
    assert "start_line" not in loc


# --- reporter: SARIF rule defaultConfiguration level ------------------------


def test_sarif_rule_default_level_maps_info_to_note() -> None:
    """SARIF's ``defaultConfiguration.level`` is derived from the first
    violation's severity for each rule id. An ``info`` severity maps to
    SARIF ``note`` (per ``_SARIF_LEVEL``)."""
    module = netlist.load(_BAD_JSON) if _BAD_JSON.exists() else None
    if module is None:
        pytest.skip(f"fixture not built: {_BAD_JSON}")
    info_v = Violation(rule_id="CDC-099", severity="info", message="fyi")
    result = AnalysisResult(
        module=module,
        domains=assign_domains(module),
        crossings=[],
        async_crossings=[],
        spec=None,
        violations=[info_v],
    )
    buf = io.StringIO()
    render_sarif(result, buf)
    data = json.loads(buf.getvalue())
    rules = data["runs"][0]["tool"]["driver"]["rules"]
    cdc_099 = next(r for r in rules if r["id"] == "CDC-099")
    assert cdc_099["defaultConfiguration"]["level"] == "note"
    assert data["runs"][0]["results"][0]["level"] == "note"


# --- render: ports outside clusters + orphan / no-period / undeclared -------


def test_render_unclocked_and_mystery_ports_outside_clusters() -> None:
    """Ports with no ``clock`` (or a clock unknown to ``clock_class``)
    are emitted outside every subgraph with the fallback grey class.
    A port whose clock matches an orphan flop's undeclared clock is
    pulled INTO that orphan subgraph instead."""
    out = render_mermaid(
        {
            "schema_version": "1.0",
            "design": {"top": "t"},
            "clocks": [{"name": "ca", "period": 10.0}],
            "generated_clocks": [],
            "flop_domains": [
                {"instance_path": "t.fa", "clock": "ca"},
                {"instance_path": "t.forphan", "clock": "orphan_clk"},
            ],
            "port_domains": [
                # No clock at all -> outside cluster, fallback grey.
                {"port": "p_unclk", "kind": "input"},
                # Clock unknown to clock_class -> also outside.
                {"port": "p_mystery", "clock": "nope", "kind": "output"},
                # Clock matches the orphan flop's clock -> inside the
                # synthetic orphan subgraph.
                {"port": "p_orphan", "clock": "orphan_clk", "kind": "input"},
            ],
            "crossings": [],
        }
    )
    # Orphan flop's clock gets an "(undeclared)" subgraph.
    assert "orphan_clk (undeclared)" in out
    # The orphan-clocked port sits inside that subgraph's block.
    assert "p_in_p_orphan" in out
    # Unclocked + mystery ports rendered with the fallback class.
    assert ":::port_unassigned" in out
    assert "p_in_p_unclk" in out
    assert "p_ou_p_mystery" in out
    # The mystery-clock port is *not* tucked into any clock subgraph,
    # so its line is emitted at the 2-space top-level indent.
    assert "  p_ou_p_mystery" in out


def test_render_orphan_clock_subgraph_and_legend_label() -> None:
    """An orphan clock (referenced by a flop but absent from
    ``clocks``/``generated_clocks``) renders both an ``(undeclared)``
    subgraph AND, because there are ≥2 palette entries, a legend entry
    labelled ``flop · <clk> (undeclared)``."""
    out = render_mermaid(
        {
            "schema_version": "1.0",
            "design": {"top": "t"},
            "clocks": [{"name": "ca", "period": 10.0}],
            "generated_clocks": [],
            "flop_domains": [
                {"instance_path": "t.fa", "clock": "ca"},
                {"instance_path": "t.fb", "clock": "weird_clk"},
            ],
            "port_domains": [],
            "crossings": [],
        }
    )
    # The orphan-clock subgraph.
    assert 'subgraph clk_weird_clk["weird_clk (undeclared)"]' in out
    # And the legend's orphan label (line 206 branch).
    assert "flop · weird_clk (undeclared)" in out


def test_render_legend_label_for_clock_without_period() -> None:
    """A declared clock with no ``period`` falls through to the bare
    ``flop · <name>`` legend label (neither the orphan branch nor the
    ``period``-bearing branch)."""
    out = render_mermaid(
        {
            "schema_version": "1.0",
            "design": {"top": "t"},
            "clocks": [
                {"name": "ca", "period": 10.0},
                {"name": "cb"},  # no period
            ],
            "generated_clocks": [],
            "flop_domains": [
                {"instance_path": "t.fa", "clock": "ca"},
                {"instance_path": "t.fb", "clock": "cb"},
            ],
            "port_domains": [],
            "crossings": [],
        }
    )
    # Two clocks => legend emitted. The period-less clock gets a plain
    # label, while the period-bearing one shows its ns.
    assert "flop · cb\n" in out or 'flop · cb"' in out
    assert "flop · ca (10.0 ns)" in out


# --- render: crossing source branches ---------------------------------------


def test_render_port_sourced_crossing_edge() -> None:
    """A crossing whose source is a typed input port (``src_port``, no
    ``src_flop``) draws its edge from the port node id."""
    out = render_mermaid(
        {
            "schema_version": "1.0",
            "design": {"top": "t"},
            "clocks": [
                {"name": "ca", "period": 10.0},
                {"name": "cb", "period": 10.0},
            ],
            "generated_clocks": [],
            "flop_domains": [{"instance_path": "t.fb", "clock": "cb"}],
            "port_domains": [{"port": "in0", "clock": "ca", "kind": "input"}],
            "crossings": [
                {
                    "src_clock": "ca",
                    "dst_clock": "cb",
                    "src_port": "in0",
                    "dst_flop": "t.fb",
                    "width": 1,
                    "async_per_sdc": True,
                }
            ],
        }
    )
    # The async edge originates at the port node (p_in_in0), not a flop.
    edge = next(
        line
        for line in out.splitlines()
        if "⚠ async" in line and "lg_async" not in line
    )
    assert edge.lstrip().startswith("p_in_in0")
    assert ".-> f_" in edge  # to the hashed destination-flop node


def test_render_skips_crossing_without_dst_or_source() -> None:
    """Malformed crossings are silently dropped: one with no
    ``dst_flop`` and one with neither ``src_flop`` nor ``src_port``
    produce no edges, leaving only the single well-formed crossing."""
    out = render_mermaid(
        {
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
                # No dst_flop -> dropped.
                {"src_clock": "ca", "src_flop": "t.fa", "width": 1},
                # Neither src_flop nor src_port -> dropped.
                {"dst_flop": "t.fb", "src_clock": "ca", "dst_clock": "cb"},
                # Well-formed sync crossing -> a single plain link.
                {
                    "src_clock": "ca",
                    "dst_clock": "cb",
                    "src_flop": "t.fa",
                    "dst_flop": "t.fb",
                    "width": 1,
                    "async_per_sdc": False,
                },
            ],
        }
    )
    plain_links = [line for line in out.splitlines() if " --- " in line]
    # Exactly the one well-formed sync crossing survives (the legend's
    # demonstrator link uses lg_sync_* ids).
    real_links = [line for line in plain_links if "lg_sync" not in line]
    assert len(real_links) == 1
    # No real async edge survives — the only ⚠ async motif left is the
    # legend's demonstrator (placeholder lg_async_* ids).
    real_async = [
        line
        for line in out.splitlines()
        if "⚠ async" in line and "lg_async" not in line
    ]
    assert real_async == []


def test_render_skips_malformed_clock_network_crossing() -> None:
    """A ``clock_network_crossings`` entry missing ``src_flop``/
    ``dst_flop`` is dropped — no thick ``==>`` arrow, no ``linkStyle``
    amber directive (the index list stays empty)."""
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
                # Missing both endpoints -> dropped.
                {"control_cell": "$mux$1", "control_pin": "S"},
            ],
        }
    )
    assert "==>" not in out
    assert "clk-ctrl" not in out
    assert "linkStyle" not in out


# --- render: pure helpers ---------------------------------------------------


def test_clock_header_without_period_returns_name() -> None:
    """``_clock_header`` returns the bare name when ``period`` is absent
    or non-numeric (the ``isinstance(period, (int, float))`` guard
    fails)."""
    assert _clock_header({"name": "ck"}) == "ck"
    assert _clock_header({"name": "ck", "period": None}) == "ck"
    assert _clock_header({"name": "ck", "period": "fast"}) == "ck"
    # With a numeric period the formatted form is used.
    assert _clock_header({"name": "ck", "period": 12.5}) == "ck · 12.5 ns"


def test_safe_id_escapes_special_chars_and_leading_digit() -> None:
    """``_safe_id`` maps each non-alnum/underscore char to ``_`` and
    prepends a leading underscore when the result wouldn't start with a
    letter or underscore."""
    # Slash -> underscore; still starts with a letter.
    assert _safe_id("clk/0") == "clk_0"
    # Several specials collapse to underscores.
    assert _safe_id("a.b/c-d") == "a_b_c_d"
    # Leading digit -> a leading underscore is inserted.
    assert _safe_id("0clk") == "_0clk"
    # Empty / all-special -> the inserted leading underscore keeps it
    # id-like.
    assert _safe_id("").startswith("_")
    assert _safe_id("$$").startswith("_")


def test_render_rejects_non_v1_schema() -> None:
    """The schema guard raises ``RenderError`` for a major-version
    mismatch, mirrored here so this file is self-contained."""
    with pytest.raises(RenderError, match="unsupported"):
        render_mermaid({"schema_version": "3.0"})
