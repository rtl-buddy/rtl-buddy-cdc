"""Two-backend SDC reader: parity, evaluation reach, and safety (#298).

:mod:`rtl_buddy_cdc.sdc` reads a constraints file with one of two
Layer-1 backends:

``tcl``
    ``tkinter.Tcl()`` → ``interp create -safe`` → an ``unknown``
    handler aliased back into Python. Real Tcl evaluation.

``tokenizer``
    :func:`rtl_buddy_cdc.tcl_tokenizer._tokenize`, the hand-written
    word splitter, used when ``_tkinter`` is not importable.

Three groups of tests live here:

1. **Parity** — constructs both readers must agree on (``\\``
   continuation, ``#`` inside braces, ``-group`` collections).
2. **Reach** — constructs only the Tcl reader can evaluate (``$var``,
   ``[expr]``, ``{10.0}`` as a period, nested ``[get_pins [get_cells
   …]/C]``). These are ``xfail(strict=True)`` on the tokenizer, which
   is the documented subset boundary, plus an assertion that the
   tokenizer says so via the ``sdc.tokenizer_skipped`` warning.
3. **Safety** — an SDC is untrusted input. Neither reader may execute
   ``exec`` / ``open`` / ``file delete`` / ``source``; the safe interp
   does not define them at all, so they land in ``unknown`` with
   everything else and are reported, never run.
"""

from __future__ import annotations

import ast
import logging
import time
from pathlib import Path

import pytest

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.sdc import (
    BACKEND_ENV_VAR,
    BACKENDS,
    TKINTER_AVAILABLE,
    TclReadError,
    TclResourceLimitError,
    _at_line,
    _read_tcl,
    _strip_collection_wrappers,
    backend,
    backend_description,
    parse,
    parse_file,
)

needs_tcl = pytest.mark.skipif(
    not TKINTER_AVAILABLE,
    reason="_tkinter is not importable; the Tcl SDC backend is unavailable",
)

#: Parametrisation for a construct the tokenizer cannot evaluate. The
#: ``strict`` xfail is the point: if the tokenizer ever learns to
#: evaluate these, this file must be revisited rather than silently
#: passing.
tcl_only = pytest.mark.parametrize(
    "reader",
    [
        pytest.param("tcl", marks=needs_tcl),
        pytest.param(
            "tokenizer",
            marks=pytest.mark.xfail(
                strict=True,
                reason="the word tokenizer does not evaluate $var / expr / braces",
            ),
        ),
    ],
)


# ---- 1. parity --------------------------------------------------------------


CONTINUATION_SDC = """
create_clock -name clk \\
             -period 10 \\
             [get_ports clk]
"""

HASH_IN_BRACES_SDC = """
create_clock -name clk -period 10 -comment {# this is not a comment} \\
    [get_ports clk]
"""

CLOCK_GROUPS_SDC = """
create_clock -name a -period 10 [get_ports a]
create_clock -name b -period 10 [get_ports b]
create_clock -name c -period 12 [get_ports c]
set_clock_groups -asynchronous -group {a b} -group [get_clocks c]
"""


def test_backslash_continuation_inside_create_clock(sdc_backend) -> None:
    """A command split across three lines with ``\\`` is one command."""
    spec = parse(CONTINUATION_SDC)
    assert spec.clocks["clk"].period == 10.0
    assert spec.clocks["clk"].ports == ("clk",)


def test_hash_inside_braces_is_not_a_comment(sdc_backend) -> None:
    """``#`` only starts a comment at a word boundary — inside a braced
    word it is literal, and the continuation to the target survives."""
    spec = parse(HASH_IN_BRACES_SDC)
    assert spec.clocks["clk"].ports == ("clk",)


def test_clock_groups_mixed_brace_and_collection_forms(sdc_backend) -> None:
    """``-group {a b}`` (brace list) and ``-group [get_clocks c]``
    (collection) in one statement, read identically by both backends."""
    spec = parse(CLOCK_GROUPS_SDC)
    assert spec.async_groups == [[{"a", "b"}, {"c"}]]
    assert spec.are_async("a", "c")
    assert not spec.are_async("a", "b")


def test_parse_file_honours_the_backend_argument(tmp_path, sdc_backend) -> None:
    """``parse_file`` forwards ``backend=`` the same way ``parse`` does."""
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n")
    spec = parse_file(sdc, backend=sdc_backend)
    assert spec.clocks["clk"].period == 10.0


# ---- 2. reach: what only the Tcl reader evaluates ---------------------------


BRACE_PERIOD_SDC = "create_clock -name clk -period {10.0} [get_ports clk]\n"

VAR_PERIOD_SDC = """
set p 10
create_clock -name clk -period [expr {$p*2}] [get_ports clk]
"""

NESTED_COLLECTION_SDC = """
create_clock -name ck_a -period 10 [get_ports ck_a]
create_generated_clock -name gclk -source [get_ports ck_a] -divide_by 2 \\
    [get_pins [get_cells u_div]/C]
"""


@tcl_only
def test_braced_period_operand(reader) -> None:
    """``-period {10.0}``: Tcl strips the braces, the tokenizer keeps
    them and ``float("{10.0}")`` fails, so the clock is dropped."""
    spec = parse(BRACE_PERIOD_SDC, backend=reader)
    assert spec.clocks["clk"].period == 10.0


@tcl_only
def test_period_from_variable_and_expr(reader) -> None:
    """The motivating case from #298: a period computed from a ``set``
    variable through ``[expr]``."""
    spec = parse(VAR_PERIOD_SDC, backend=reader)
    assert spec.clocks["clk"].period == 20.0
    assert spec.clocks["clk"].ports == ("clk",)


@tcl_only
def test_nested_get_pins_get_cells_collapses_to_one_pin(reader) -> None:
    """``[get_pins [get_cells u_div]/C]`` evaluates inside-out to the
    single pin path ``u_div/C``. The tokenizer treats the whole bracket
    span as opaque text and splits it into two bogus names."""
    spec = parse(NESTED_COLLECTION_SDC, backend=reader)
    assert spec.pin_clocks == {"u_div/C": "gclk"}


def test_tokenizer_warns_once_when_it_drops_a_dollar_word(caplog) -> None:
    """The ``sdc.tokenizer_skipped`` warning fires the first time a
    ``$``-word survives into a CDC-relevant command, and only once."""
    text = (
        "create_clock -name a -period $pa [get_ports a]\n"
        "create_clock -name b -period $pb [get_ports b]\n"
    )
    with caplog.at_level(logging.WARNING, logger="rtl_buddy_cdc.sdc"):
        parse(text, backend="tokenizer")
    hits = [r for r in caplog.records if "sdc.tokenizer_skipped" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    assert "$" in hits[0].getMessage()


def test_tokenizer_warns_once_when_it_drops_an_unknown_command(caplog) -> None:
    """``set p 10`` is a command the tokenizer cannot act on — same
    once-per-file warning."""
    with caplog.at_level(logging.WARNING, logger="rtl_buddy_cdc.sdc"):
        parse(VAR_PERIOD_SDC, backend="tokenizer")
    hits = [r for r in caplog.records if "sdc.tokenizer_skipped" in r.getMessage()]
    assert len(hits) == 1
    assert "'set'" in hits[0].getMessage()


@needs_tcl
def test_tcl_backend_does_not_emit_the_tokenizer_skip_warning(caplog) -> None:
    """Nothing is skipped when the constructs are actually evaluated."""
    with caplog.at_level(logging.WARNING, logger="rtl_buddy_cdc.sdc"):
        parse(VAR_PERIOD_SDC, backend="tcl")
    assert not [r for r in caplog.records if "sdc.tokenizer_skipped" in r.getMessage()]


# ---- 3. safety: an SDC is untrusted input -----------------------------------


def test_exec_never_spawns_a_process(tmp_path, sdc_backend) -> None:
    sentinel = tmp_path / "exec_sentinel"
    spec = parse(f"exec touch {sentinel}\n")
    assert not sentinel.exists()
    assert any("exec" in w for w in spec.partial_warnings), spec.partial_warnings


def test_open_never_creates_a_file(tmp_path, sdc_backend) -> None:
    sentinel = tmp_path / "open_sentinel"
    spec = parse(f"open {sentinel} w\n")
    assert not sentinel.exists()
    assert any("open" in w for w in spec.partial_warnings), spec.partial_warnings


def test_file_delete_never_removes_a_file(tmp_path, sdc_backend) -> None:
    sentinel = tmp_path / "keep_me"
    sentinel.write_text("still here")
    spec = parse(f"file delete {sentinel}\n")
    assert sentinel.read_text() == "still here"
    assert any("file" in w for w in spec.partial_warnings), spec.partial_warnings


def test_source_is_refused_with_an_explicit_diagnostic(tmp_path, sdc_backend) -> None:
    """``source`` cannot pull in a second file; say so by name rather
    than silently producing a clock-free spec."""
    included = tmp_path / "inc.sdc"
    included.write_text("create_clock -name sneaky -period 10 [get_ports sneaky]\n")
    spec = parse(
        f"source {included}\ncreate_clock -name clk -period 10 [get_ports clk]\n"
    )
    assert "sneaky" not in spec.clocks
    assert "clk" in spec.clocks, "parsing continues past the refused source"
    refusals = [w for w in spec.partial_warnings if w.startswith("source ")]
    assert len(refusals) == 1
    assert "are not supported" in refusals[0]
    assert str(included) in refusals[0]


@needs_tcl
def test_tcl_backend_reports_the_line_of_a_refused_command() -> None:
    """``info frame`` gives the interp reader real line numbers."""
    spec = parse("\n\nsource other.sdc\n", backend="tcl")
    assert spec.partial_warnings == [
        "source 'other.sdc' (line 3): " + sdc_mod._UNSUPPORTED_COMMANDS["source"]
    ]


def test_tokenizer_backend_omits_the_line_it_does_not_know() -> None:
    """The tokenizer tracks no line numbers, so ``_Command.line`` is
    ``None`` and the diagnostic simply drops the suffix."""
    spec = parse("\n\nsource other.sdc\n", backend="tokenizer")
    assert spec.partial_warnings == [
        "source 'other.sdc': " + sdc_mod._UNSUPPORTED_COMMANDS["source"]
    ]


def test_at_line_tolerates_none() -> None:
    assert _at_line(None) == ""
    assert _at_line(7) == " (line 7)"


# ---- backend selection ------------------------------------------------------


def test_backend_env_var_forces_the_tokenizer(monkeypatch) -> None:
    monkeypatch.setenv(BACKEND_ENV_VAR, "TOKENIZER")
    assert backend() == "tokenizer"
    assert "tokenizer" in backend_description()


def test_backend_auto_selects_tcl_when_available(monkeypatch) -> None:
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    assert backend() == ("tcl" if TKINTER_AVAILABLE else "tokenizer")


def test_blank_backend_env_var_falls_through_to_auto(monkeypatch) -> None:
    monkeypatch.setenv(BACKEND_ENV_VAR, "   ")
    assert backend() in BACKENDS


def test_unknown_backend_name_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="unknown SDC backend"):
        parse("", backend="verilog")


def test_requesting_tcl_without_tkinter_degrades_and_warns(monkeypatch, caplog) -> None:
    """The fallback is a warning, not an exception — the analysis still
    runs, just with the documented subset."""
    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)
    monkeypatch.setattr(sdc_mod, "TKINTER_AVAILABLE", False)
    monkeypatch.setattr(sdc_mod, "_warned_tcl_unavailable", False)
    with caplog.at_level(logging.WARNING, logger="rtl_buddy_cdc.sdc"):
        assert sdc_mod._resolve_backend("tcl") == "tokenizer"
        # Auto-selection and an explicit tokenizer request warn too,
        # but the latch keeps it to one line per run.
        assert sdc_mod._resolve_backend(None) == "tokenizer"
        assert sdc_mod._resolve_backend("tokenizer") == "tokenizer"
    hits = [r for r in caplog.records if "sdc.tcl_unavailable" in r.getMessage()]
    assert len(hits) == 1
    assert "python3-tkinter" in hits[0].getMessage()
    assert "tokenizer (_tkinter not importable" in backend_description()


def test_reset_backend_warnings_rearms_the_latch(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sdc_mod, "TKINTER_AVAILABLE", False)
    monkeypatch.setattr(sdc_mod, "_warned_tcl_unavailable", True)
    sdc_mod._reset_backend_warnings()
    with caplog.at_level(logging.WARNING, logger="rtl_buddy_cdc.sdc"):
        sdc_mod._warn_tcl_unavailable()
    assert any("sdc.tcl_unavailable" in r.getMessage() for r in caplog.records)


@needs_tcl
def test_backend_description_names_the_forced_tokenizer(monkeypatch) -> None:
    monkeypatch.setenv(BACKEND_ENV_VAR, "tokenizer")
    assert "forced via" in backend_description()


# ---- Tcl reader internals ---------------------------------------------------


@needs_tcl
def test_read_tcl_records_words_and_lines() -> None:
    commands = _read_tcl(
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "set_clock_groups -asynchronous -group {a b} -group [get_clocks c]\n"
    )
    assert [c.words for c in commands] == [
        ["create_clock", "-name", "clk", "-period", "10", "[get_ports clk]"],
        [
            "set_clock_groups",
            "-asynchronous",
            "-group",
            "a b",
            "-group",
            "[get_clocks c]",
        ],
    ]
    assert [c.line for c in commands] == [1, 2]


@needs_tcl
def test_read_tcl_re_wraps_empty_collections() -> None:
    """``[all_inputs]`` has no operands; the canonical bracket form is
    still what reaches ``_extract_names``, same as the tokenizer's."""
    (command,) = _read_tcl("set_input_delay -clock clk 1.0 [all_inputs]\n")
    assert command.words[-1] == "[all_inputs]"


@needs_tcl
def test_read_tcl_tolerates_a_frame_without_a_line() -> None:
    """``info frame`` does not always hand back a usable line (a
    dynamically built script, an SDC that installs its own ``unknown``).
    The reader degrades to ``line=None``; ``_at_line`` then renders
    nothing rather than a bogus location."""
    (command,) = _read_tcl(
        "proc unknown args { return [rb_cdc_record {} {*}$args] }\n"
        "create_clock -name clk -period 10 [get_ports clk]\n"
    )
    assert command.words[0] == "create_clock"
    assert command.line is None


@needs_tcl
def test_read_tcl_raises_on_a_tcl_error() -> None:
    with pytest.raises(TclReadError):
        _read_tcl("create_clock -name clk -period $undefined_variable\n")


@needs_tcl
def test_parse_falls_back_to_the_tokenizer_on_a_tcl_error() -> None:
    """An unevaluable file must still yield whatever the tokenizer can
    see, with the degradation recorded rather than swallowed."""
    spec = parse(
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "set_false_path -from [get_clocks $nope] -to [get_clocks clk]\n",
        backend="tcl",
    )
    assert "clk" in spec.clocks, "the tokenizer re-read recovered the good command"
    assert any(
        "could not evaluate this file" in w and "word tokenizer" in w
        for w in spec.partial_warnings
    ), spec.partial_warnings


# ---- resource limits: a constraints file must not be able to hang us ------
#
# ``interp create -safe`` bounds what the child *can do*, not what it
# *costs*. Without an ``interp limit`` a one-line SDC holds the process
# inside Tcl_EvalEx forever, where Python cannot interrupt it.


@needs_tcl
def test_infinite_loop_is_cut_off_by_the_time_limit(monkeypatch) -> None:
    """``while 1 {}`` dispatches no commands, so only the wall-clock
    deadline stops it. The production budget is 30s; squeeze it to 1s
    here so the test costs a second rather than half a minute."""
    monkeypatch.setattr(sdc_mod, "TCL_TIME_LIMIT_SECONDS", 1)
    started = time.monotonic()
    spec = parse(
        "create_clock -name clk -period 10 [get_ports clk]\nwhile 1 {}\n",
        backend="tcl",
    )
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"parse() did not return promptly ({elapsed:.1f}s)"
    assert any(
        "hit a resource limit" in w and "cut off part-way" in w
        for w in spec.partial_warnings
    ), spec.partial_warnings
    # The tokenizer re-read still recovers everything it can see.
    assert spec.clocks["clk"].period == 10.0


@needs_tcl
def test_runaway_loop_is_cut_off_by_the_command_limit() -> None:
    """A loop with a non-empty body trips the command counter long
    before the wall-clock deadline — no monkeypatching needed."""
    started = time.monotonic()
    spec = parse("set i 0\nwhile {$i < 100000000} {incr i}\n", backend="tcl")
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"parse() did not return promptly ({elapsed:.1f}s)"
    assert any("hit a resource limit" in w for w in spec.partial_warnings), (
        spec.partial_warnings
    )


@needs_tcl
def test_read_tcl_raises_the_resource_limit_subclass(monkeypatch) -> None:
    """The limit case is a distinguishable ``TclReadError`` subclass so
    ``parse`` can word its diagnostic differently."""
    monkeypatch.setattr(sdc_mod, "TCL_COMMAND_LIMIT", 100)
    with pytest.raises(TclResourceLimitError):
        _read_tcl("set i 0\nwhile {$i < 100000} {incr i}\n")


@needs_tcl
def test_resource_limits_do_not_disturb_an_ordinary_file() -> None:
    """The limits are armed before the bootstrap script, so they have
    to be generous enough for it plus a real constraints file."""
    spec = parse(CLOCK_GROUPS_SDC, backend="tcl")
    assert spec.partial_warnings == []
    assert set(spec.clocks) == {"a", "b", "c"}


def test_strip_collection_wrappers_peels_nested_forms() -> None:
    assert _strip_collection_wrappers("[get_cells u/*]/C") == "u/*/C"
    assert _strip_collection_wrappers("[get_pins [get_cells a]/C]") == "a/C"
    assert _strip_collection_wrappers("plain") == "plain"


@needs_tcl
def test_filter_clauses_still_warn_under_the_tcl_reader() -> None:
    """``-filter`` is out of scope on both readers; the Tcl reader must
    hand ``_extract_names`` a word it still recognises as filtered."""
    spec = parse(
        "create_clock -name clk -period 10 [get_ports -filter {DIRECTION == in} clk]\n",
        backend="tcl",
    )
    assert any("filter" in w for w in spec.partial_warnings), spec.partial_warnings


# ---- vendoring contract -----------------------------------------------------


def test_tcl_tokenizer_imports_nothing_from_the_package() -> None:
    """rtl-buddy vendors ``tcl_tokenizer.py`` verbatim (rtl_buddy#641),
    so it must stay stdlib-only and package-free."""
    source = Path(sdc_mod.__file__).with_name("tcl_tokenizer.py").read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [m for m in imported if m.split(".")[0] == "rtl_buddy_cdc"], imported
    # ``__future__`` is the only import the file needs at all.
    assert imported == ["__future__"], imported
