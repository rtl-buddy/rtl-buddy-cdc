"""Clock collections in clock positions (rtl-buddy-cdc#390).

Before #390 every clock position (``set_clock_groups -group``, the clock
endpoints of ``set_false_path`` / ``set_max_delay -datapath_only``,
``-clock``, ``-master_clock``) peeled its operand with the tokenizer's
``_extract_names``, which reads any bracketed command head it does not
know as a *name*: ``[all_clocks]`` became a clock called ``all_clocks``,
``[all_registers]`` one called ``all_registers``.

Now a clock position reads clock collections:

- ``[all_clocks]`` is every clock the file declares (generated and
  virtual clocks included). Clock references are resolved against the
  whole file, as every other clock name already is (a ``set_*_delay
  -clock`` or ``-group`` may name a clock declared further down), so a
  clock declared *below* the statement is included too — with a warning,
  because a timer runs the file in order and would not include it.
- ``[get_clocks <glob>]`` expands against the same set.
- ``[all_registers]`` / ``[all_inputs]`` / ``[all_outputs]`` /
  ``[get_cells …]`` / ``[get_nets …]`` name no clock: they select nothing,
  with a ``partial_warnings`` entry.
- A single-clock option (``-clock``, ``-master_clock``) takes a
  collection that resolves to exactly one clock; anything else is ignored
  with a warning.

Every test runs on both SDC reader backends, and every companion operand
is written in the bracket / brace / bare forms.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.sdc import ClockSpec, SdcObject, parse

needs_tcl = pytest.mark.skipif(
    not sdc_mod.tcl_available(), reason="_tkinter unavailable: no Tcl backend"
)

FORMS = pytest.mark.parametrize(
    "form",
    ["[get_clocks {name}]", "{{{name}}}", "{name}"],
    ids=["bracket", "brace", "bare"],
)

_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 5 [get_ports clk_b]
create_generated_clock -name clk_g -source [get_ports clk_a] -master_clock clk_a \
    -divide_by 2 [get_pins u_div/q]
create_clock -name vclk -period 8
"""

_ALL = {"clk_a", "clk_b", "clk_g", "vclk"}


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    return sdc_backend


def _no_line(warnings: list[str]) -> list[str]:
    return [re.sub(r" \(line \d+\)", "", w) for w in warnings]


def _form(form: str, name: str) -> str:
    return form.format(name=name)


# --- 3. all_clocks / all_registers in a clock position -----------------------


@FORMS
def test_all_clocks_group_is_every_declared_clock(form: str) -> None:
    spec = parse(
        _CLOCKS + f"set_clock_groups -physically_exclusive "
        f"-group {_form(form, 'clk_b')} -group [all_clocks]\n"
    )
    assert spec.exclusive_groups == [[{"clk_b"}, _ALL]]
    assert spec.partial_warnings == []


@FORMS
def test_all_clocks_false_path_endpoint(form: str) -> None:
    spec = parse(
        _CLOCKS + f"set_false_path -from [all_clocks] -to {_form(form, 'clk_b')}\n"
    )
    assert spec.false_path_pairs == {
        frozenset({"clk_a", "clk_b"}),
        frozenset({"clk_g", "clk_b"}),
        frozenset({"vclk", "clk_b"}),
    }
    assert "all_clocks" not in {c for pair in spec.false_path_pairs for c in pair}
    assert spec.partial_warnings == []


@FORMS
def test_all_clocks_datapath_only_endpoint(form: str) -> None:
    spec = parse(
        _CLOCKS
        + f"set_max_delay 3 -datapath_only -from {_form(form, 'clk_a')} -to [all_clocks]\n"
    )
    (record,) = spec.datapath_only_delays
    assert record.from_clocks == ("clk_a",)
    assert set(record.to_clocks) == _ALL
    assert frozenset({"clk_a", "clk_b"}) in spec.datapath_only_pairs
    assert spec.partial_warnings == []


def test_all_clocks_includes_clocks_declared_later_with_a_warning() -> None:
    """Clock references resolve against the whole file (a ``-group`` name
    may be declared below it), so ``[all_clocks]`` does too — including a
    generated clock declared later — and says so, since a timer evaluates
    it in file order."""
    spec = parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "set_clock_groups -asynchronous -group clk_x -group [all_clocks]\n"
        "create_clock -name clk_x -period 3 [get_ports clk_x]\n"
        "create_generated_clock -name clk_g -source [get_ports clk_a] "
        "-master_clock clk_a -divide_by 2 [get_pins u/q]\n"
    )
    assert spec.async_groups == [[{"clk_x"}, {"clk_a", "clk_x", "clk_g"}]]
    (warning,) = _no_line(spec.partial_warnings)
    assert warning.startswith("set_clock_groups:")
    assert "clk_x, clk_g" in warning and "declared later" in warning


def test_all_clocks_with_no_clock_declared_selects_nothing() -> None:
    spec = parse("set_clock_groups -asynchronous -group a -group [all_clocks]\n")
    assert spec.async_groups == []
    assert any("matches no declared clock" in w for w in spec.partial_warnings)


@pytest.mark.parametrize(
    "operand",
    [
        "[all_registers]",
        "[all_inputs]",
        "[all_outputs]",
        "[get_cells u/q_reg]",
        "[get_nets n]",
    ],
    ids=["all_registers", "all_inputs", "all_outputs", "get_cells", "get_nets"],
)
def test_non_clock_collection_in_a_group_selects_nothing(operand: str) -> None:
    spec = parse(
        _CLOCKS + f"set_clock_groups -asynchronous -group clk_a -group {operand} "
        "-group {clk_b}\n"
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b"}]]
    names = {c for group in spec.async_groups[0] for c in group}
    assert not names & {"all_registers", "all_inputs", "all_outputs", "get_cells"}
    (warning,) = _no_line(spec.partial_warnings)
    assert "is not a clock collection" in warning


@FORMS
def test_all_registers_false_path_endpoint_is_ignored(form: str) -> None:
    spec = parse(
        _CLOCKS + f"set_false_path -from [all_registers] -to {_form(form, 'clk_b')}\n"
    )
    assert spec.false_path_pairs == set()
    assert spec.false_path_exceptions == []
    assert any("is not a clock collection" in w for w in spec.partial_warnings)


def test_all_registers_datapath_only_endpoint_is_ignored() -> None:
    spec = parse(
        _CLOCKS + "set_max_delay 3 -datapath_only -from [all_registers] -to clk_b\n"
    )
    assert spec.datapath_only_delays == [] and spec.datapath_only_pairs == set()
    assert any("is not a clock collection" in w for w in spec.partial_warnings)


def test_all_registers_beside_an_object_endpoint_keeps_the_exception_narrow() -> None:
    """A side that held operands but selected nothing must not read as
    "any object": the statement is dropped rather than widened."""
    spec = parse(
        _CLOCKS + "set_false_path -from [get_cells u/q_reg] -to [all_registers]\n"
    )
    assert spec.false_path_exceptions == []
    assert any("-to selects nothing" in w for w in spec.partial_warnings)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("clk_*", {"clk_a", "clk_b", "clk_g"}), ("clk_?", {"clk_a", "clk_b", "clk_g"})],
    ids=["star", "question"],
)
def test_get_clocks_glob_expands(pattern: str, expected: set[str]) -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group vclk "
        f"-group [get_clocks {pattern}]\n"
    )
    assert spec.async_groups == [[{"vclk"}, expected]]


def test_get_clocks_glob_matching_nothing_warns() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group vclk -group clk_a "
        "-group [get_clocks nope*]\n"
    )
    assert spec.async_groups == [[{"vclk"}, {"clk_a"}]]
    assert any("'nope*' matches no declared clock" in w for w in spec.partial_warnings)


def test_filter_in_a_group_is_reported() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group clk_a "
        "-group [get_clocks -filter {NAME =~ x*}]\n"
    )
    assert spec.async_groups == []
    assert any("-filter" in w for w in spec.partial_warnings)


# --- single-clock options ------------------------------------------------------


def test_delay_clock_all_clocks_with_several_clocks_is_ignored() -> None:
    spec = parse(_CLOCKS + "set_input_delay 1 -clock [all_clocks] [get_ports d]\n")
    assert spec.port_clock == {} and spec.port_delays == []
    assert any("several clocks" in w for w in spec.partial_warnings)
    assert "all_clocks" not in spec.port_clock.values()


def test_delay_clock_all_clocks_with_one_clock_is_that_clock() -> None:
    spec = parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "set_input_delay 1 -clock [all_clocks] [get_ports d]\n"
    )
    assert spec.port_clock == {"d": "clk_a"}
    assert spec.partial_warnings == []


@FORMS
def test_delay_clock_forms_are_unchanged(form: str) -> None:
    spec = parse(_CLOCKS + f"set_input_delay 1 -clock {_form(form, 'clk_b')} d\n")
    assert spec.port_clock == {"d": "clk_b"}
    assert spec.partial_warnings == []


def test_delay_clock_list_is_ignored() -> None:
    spec = parse(_CLOCKS + "set_input_delay 1 -clock {clk_a clk_b} d\n")
    assert spec.port_clock == {}
    assert any("several clocks" in w for w in spec.partial_warnings)


def test_delay_clock_non_clock_collection_is_ignored() -> None:
    spec = parse(_CLOCKS + "set_input_delay 1 -clock [all_registers] d\n")
    assert spec.port_clock == {}
    assert any("is not a clock collection" in w for w in spec.partial_warnings)
    assert any("no -clock anchor" in w for w in spec.partial_warnings)


def test_master_clock_all_clocks_is_not_a_master() -> None:
    spec = parse(
        _CLOCKS + "create_generated_clock -name clk_h -source [get_ports clk_b] "
        "-master_clock [all_clocks] -divide_by 2 [get_pins u_h/q]\n"
    )
    assert spec.clocks["clk_h"].master is None
    assert any("several clocks" in w for w in spec.partial_warnings)


@FORMS
def test_master_clock_forms_are_unchanged(form: str) -> None:
    spec = parse(
        _CLOCKS + "create_generated_clock -name clk_h -source [get_ports clk_b] "
        f"-master_clock {_form(form, 'clk_b')} -divide_by 2 [get_pins u_h/q]\n"
    )
    assert spec.clocks["clk_h"].master == "clk_b"
    assert spec.clocks["clk_h"].period == 10.0


def test_master_clock_filter_is_reported() -> None:
    spec = parse(
        _CLOCKS + "create_generated_clock -name clk_h -source [get_ports clk_b] "
        "-master_clock [get_clocks -filter {NAME == clk_b}] [get_pins u_h/q]\n"
    )
    assert spec.clocks["clk_h"].master is None
    assert any("-filter" in w for w in spec.partial_warnings)


def test_get_ports_in_a_clock_group_still_names_the_clock() -> None:
    """Pre-#390 reading kept: ``[get_ports clk]`` in a group is the clock
    created on that port without ``-name`` (its name is the port's)."""
    spec = parse(
        "create_clock -period 10 [get_ports clk_a]\n"
        "create_clock -period 5 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group [get_ports clk_a] "
        "-group [get_pins clk_b]\n"
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b"}]]


def test_hand_built_spec_resolves_against_its_own_clocks() -> None:
    """Handlers called on a spec :func:`parse` did not build fall back to
    the clocks declared on it."""
    spec = ClockSpec()
    for line in (
        "create_clock -name a -period 1 [get_ports a]",
        "create_clock -name b -period 1 [get_ports b]",
        "set_false_path -from [all_clocks] -to b",
    ):
        words = sdc_mod._tokenize(line)[0]
        parsed = sdc_mod._slice(words[1:], sdc_mod.ARG_SPECS[words[0]])
        sdc_mod._DISPATCH[words[0]](spec, parsed)
    assert spec.false_path_pairs == {frozenset({"a", "b"})}


def test_object_endpoints_are_unchanged_beside_all_clocks() -> None:
    spec = parse(
        _CLOCKS + "set_false_path -from [get_cells u/q_reg] -to [all_clocks]\n"
    )
    (exc,) = spec.false_path_exceptions
    assert exc.from_objects == (SdcObject("cell", "u/q_reg"),)
    assert set(exc.to_clocks) == _ALL


# --- 1. remove_from_collection / add_to_collection over clocks ----------------


@FORMS
def test_all_clocks_minus_one_group(form: str) -> None:
    """The idiom the issue names: the other group is every clock but one."""
    spec = parse(
        _CLOCKS + f"set_clock_groups -asynchronous -group {_form(form, 'clk_a')} "
        f"-group [remove_from_collection [all_clocks] {_form(form, 'clk_a')}]\n"
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b", "clk_g", "vclk"}]]
    assert spec.partial_warnings == []
    assert spec.are_async("clk_a", "clk_b")
    assert spec.are_async("clk_a", "vclk")


@FORMS
def test_add_to_collection_group(form: str) -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -logically_exclusive -group vclk "
        f"-group [add_to_collection [get_clocks clk_a] {_form(form, 'clk_b')}]\n"
    )
    assert spec.exclusive_groups == [[{"vclk"}, {"clk_a", "clk_b"}]]
    assert spec.partial_warnings == []


def test_add_to_collection_unique_flag_over_clocks() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group vclk "
        "-group [add_to_collection -unique [get_clocks clk_a] [get_clocks clk_a]]\n"
    )
    assert spec.async_groups == [[{"vclk"}, {"clk_a"}]]
    assert spec.partial_warnings == []


def test_nested_clock_operators() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group clk_b -group "
        "[remove_from_collection [add_to_collection [get_clocks clk_a] "
        "[get_clocks {clk_g vclk}]] [get_clocks clk_g]]\n"
    )
    assert spec.async_groups == [[{"clk_b"}, {"clk_a", "vclk"}]]


def test_removed_glob_over_clocks() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group [get_clocks clk_*] "
        "-group [remove_from_collection [all_clocks] {clk_*}]\n"
    )
    assert spec.async_groups == [[{"clk_a", "clk_b", "clk_g"}, {"vclk"}]]


def test_removing_an_undeclared_clock_warns() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group clk_a "
        "-group [remove_from_collection [all_clocks] [get_clocks clk_aa]]\n"
    )
    assert spec.async_groups == [[{"clk_a"}, _ALL]]
    (warning,) = _no_line(spec.partial_warnings)
    assert "'clk_aa' matches no declared clock" in warning
    assert "removes nothing" in warning


def test_group_left_empty_by_a_removal_is_ignored() -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group clk_a -group clk_b "
        "-group [remove_from_collection [get_clocks vclk] vclk]\n"
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b"}]]
    assert any("selects no clock" in w for w in spec.partial_warnings)


@FORMS
def test_false_path_from_every_other_clock(form: str) -> None:
    spec = parse(
        _CLOCKS + "set_false_path -from [remove_from_collection [all_clocks] "
        f"{_form(form, 'clk_b')}] -to {_form(form, 'clk_b')}\n"
    )
    assert spec.false_path_pairs == {
        frozenset({"clk_a", "clk_b"}),
        frozenset({"clk_g", "clk_b"}),
        frozenset({"vclk", "clk_b"}),
    }
    assert spec.partial_warnings == []


@FORMS
def test_datapath_only_to_every_other_clock(form: str) -> None:
    spec = parse(
        _CLOCKS + f"set_max_delay 2 -datapath_only -from {_form(form, 'clk_a')} "
        f"-to [remove_from_collection [all_clocks] {_form(form, 'clk_a')}]\n"
    )
    (record,) = spec.datapath_only_delays
    assert record.value == 2.0
    assert record.to_clocks == ("clk_b", "clk_g", "vclk")
    assert spec.partial_warnings == []


def test_object_operator_in_a_false_path_ignores_the_statement() -> None:
    """An operator over design objects is not evaluated (an object endpoint
    has no exclusion model): the side selects nothing and the statement is
    dropped, never widened."""
    spec = parse(
        _CLOCKS + "set_false_path -from [get_cells u/a_reg] "
        "-to [remove_from_collection [get_cells u/*] [get_cells u/b_reg]]\n"
    )
    assert spec.false_path_exceptions == [] and spec.false_path_pairs == set()
    assert any("is not a clock collection" in w for w in spec.partial_warnings)
    assert any("-to selects nothing" in w for w in spec.partial_warnings)


@pytest.mark.parametrize(
    ("operator", "why"),
    [
        ("remove_from_collection -intersect [all_clocks] clk_a", "-intersect"),
        ("remove_from_collection [all_clocks]", "two operands"),
        (
            "remove_from_collection [all_clocks] "
            "[remove_from_collection [all_clocks] clk_a]",
            "removal inside the removed",
        ),
        ("remove_from_collection [all_clocks] [all_registers]", "not a clock"),
    ],
    ids=["intersect", "arity", "nested-removal", "registers-operand"],
)
def test_unsupported_clock_operator_forms_select_nothing(
    operator: str, why: str
) -> None:
    spec = parse(
        _CLOCKS + f"set_clock_groups -asynchronous -group clk_a -group [{operator}]\n"
    )
    assert spec.async_groups == []
    assert any(why in w for w in spec.partial_warnings), spec.partial_warnings


def test_operator_in_a_single_clock_option_is_not_evaluated() -> None:
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock "
        "[remove_from_collection [all_clocks] {clk_b clk_g vclk}] d\n"
    )
    assert spec.port_clock == {}
    assert any("not a collection command" in w for w in spec.partial_warnings)


@needs_tcl
def test_both_backends_read_clock_operators_identically() -> None:
    text = (
        _CLOCKS + "set_clock_groups -asynchronous -group [get_clocks clk_a] "
        "-group [remove_from_collection [all_clocks] [get_clocks {clk_a vclk}]]\n"
        + "set_false_path -from [add_to_collection clk_b {vclk}] -to clk_g\n"
        + "set_max_delay 1 -datapath_only -from vclk "
        "-to [remove_from_collection [all_clocks] [get_clocks nope]]\n"
    )
    tcl = parse(text, backend="tcl")
    tok = parse(text, backend="tokenizer")
    assert tcl.async_groups == tok.async_groups
    assert tcl.false_path_pairs == tok.false_path_pairs
    assert tcl.datapath_only_delays == tok.datapath_only_delays
    assert _no_line(tcl.partial_warnings) == tok.partial_warnings
    assert len(tok.partial_warnings) == 1


@needs_tcl
def test_clock_operator_through_a_tcl_variable() -> None:
    spec = parse(
        _CLOCKS
        + "set others [remove_from_collection [all_clocks] [get_clocks clk_a]]\n"
        + "set_clock_groups -asynchronous -group clk_a -group $others\n",
        backend="tcl",
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b", "clk_g", "vclk"}]]


# --- end to end ----------------------------------------------------------------

_FOUR = Path(__file__).parent / "fixtures" / "bad_four_clock_mixed"


def _violations(tmp_path: Path, sdc_text: str) -> Counter[tuple[str, str]]:
    sdc = tmp_path / "x.sdc"
    sdc.write_text(sdc_text)
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "-n",
            str(_FOUR / "bad_four_clock_mixed.json"),
            "-s",
            str(sdc),
            "-f",
            "json",
        ],
    )
    assert result.exit_code in (0, 1), result.output
    return Counter(
        (v["rule_id"], v["message"]) for v in json.loads(result.stdout)["violations"]
    )


@pytest.mark.parametrize(
    "rest",
    [
        "[remove_from_collection [all_clocks] [get_clocks {clk_a clk_a2x clk_b}]]",
        "[remove_from_collection [all_clocks] {clk_a clk_a2x clk_b}]",
        "[remove_from_collection [get_clocks *] {clk_a* clk_b}]",
    ],
    ids=["bracket", "brace", "glob"],
)
def test_operator_group_reaches_the_rules_like_the_explicit_group(
    tmp_path: Path, rest: str
) -> None:
    """``bad_four_clock_mixed`` with its third group written as "every
    other clock" reports exactly what the explicit ``{clk_c}`` does."""
    original = (_FOUR / "bad_four_clock_mixed.sdc").read_text()
    explicit = "-group {clk_a clk_a2x} -group {clk_b} -group {clk_c}"
    assert explicit in original
    idiom = original.replace(
        explicit, "-group {clk_a clk_a2x} -group {clk_b} -group " + rest
    )
    expected = _violations(tmp_path, original)
    assert any("→ clk_c" in message for _rule, message in expected)
    assert _violations(tmp_path, idiom) == expected


# --- 2. list / concat-built collections in every command ----------------------
#
# The Tcl backend evaluates ``[list [get_ports a] [get_ports b]]`` to the
# Tcl list ``{[get_ports a]} {[get_ports b]}`` and ``concat`` to
# ``[get_ports a] [get_ports b]``; ``_extract_names`` stripped every bracket
# and brace and dropped only a *leading* ``get_ports``, so the second
# collection's head became a port called ``get_ports``. The tokenizer keeps
# ``[list …]`` opaque and used to drop it as an unrecognised command; it now
# expands ``list`` / ``concat`` the way the Tcl backend does, so both read
# the same.

BUILDERS = pytest.mark.parametrize("builder", ["list", "concat"])


def _built(builder: str, *items: str) -> str:
    return f"[{builder} {' '.join(items)}]"


@BUILDERS
def test_create_clock_built_target(builder: str) -> None:
    spec = parse(
        "create_clock -name c -period 10 "
        f"{_built(builder, '[get_ports a]', '{b}', 'c')}\n"
    )
    assert spec.clocks["c"].ports == ("a", "b", "c")
    assert spec.partial_warnings == []


@BUILDERS
def test_create_clock_built_target_names_an_unnamed_clock(builder: str) -> None:
    spec = parse(
        f"create_clock -period 10 {_built(builder, '[get_ports a]', '[get_ports b]')}\n"
    )
    assert list(spec.clocks) == ["a"]
    assert spec.clocks["a"].ports == ("a", "b")


@BUILDERS
def test_create_generated_clock_built_target_and_source(builder: str) -> None:
    spec = parse(
        _CLOCKS + "create_generated_clock -name clk_h "
        f"-source {_built(builder, '[get_ports clk_b]')} -master_clock clk_b "
        f"-divide_by 2 {_built(builder, '[get_pins u_h/q]', '[get_pins u_i/q]')}\n"
    )
    assert spec.pin_clocks["u_h/q"] == "clk_h" and spec.pin_clocks["u_i/q"] == "clk_h"
    assert "get_pins" not in spec.pin_clocks
    assert spec.clocks["clk_h"].source == ("clk_b",)
    assert spec.partial_warnings == []


@BUILDERS
def test_clock_group_built_from_collections(builder: str) -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group vclk "
        f"-group {_built(builder, '[get_clocks clk_a]', '[get_clocks clk_b]')}\n"
    )
    assert spec.async_groups == [[{"vclk"}, {"clk_a", "clk_b"}]]
    assert spec.partial_warnings == []


@BUILDERS
def test_false_path_built_endpoints(builder: str) -> None:
    spec = parse(
        _CLOCKS
        + f"set_false_path -from {_built(builder, '[get_clocks clk_a]', 'clk_g')} "
        f"-to {_built(builder, '[get_clocks clk_b]')}\n"
    )
    assert spec.false_path_pairs == {
        frozenset({"clk_a", "clk_b"}),
        frozenset({"clk_g", "clk_b"}),
    }
    assert spec.partial_warnings == []


@BUILDERS
def test_false_path_built_through(builder: str) -> None:
    spec = parse(
        _CLOCKS + "set_false_path -from [get_cells u/a_reg] "
        f"-through {_built(builder, '[get_pins u/x]', '[get_pins u/y]')}\n"
    )
    (exc,) = spec.false_path_exceptions
    assert exc.through == ((SdcObject("pin", "u/x"), SdcObject("pin", "u/y")),)
    assert spec.partial_warnings == []


@BUILDERS
def test_datapath_only_built_endpoints(builder: str) -> None:
    spec = parse(
        _CLOCKS + "set_max_delay 2 -datapath_only "
        f"-from {_built(builder, '[get_cells u/a_reg]', '[get_cells u/b_reg]')} "
        f"-to {_built(builder, '[get_clocks clk_b]')}\n"
    )
    (record,) = spec.datapath_only_delays
    assert record.from_objects == (("cell", "u/a_reg"), ("cell", "u/b_reg"))
    assert record.to_clocks == ("clk_b",)


@BUILDERS
def test_case_analysis_built_targets(builder: str) -> None:
    spec = parse(
        f"set_case_analysis 1 {_built(builder, '[get_ports m0]', '{m1}', 'm2')}\n"
    )
    assert spec.case_analysis == {"m0": "1", "m1": "1", "m2": "1"}
    assert spec.partial_warnings == []


@BUILDERS
def test_delay_built_targets_and_clock(builder: str) -> None:
    spec = parse(
        _CLOCKS + f"set_input_delay 1 -clock {_built(builder, '[get_clocks clk_a]')} "
        f"{_built(builder, '[get_ports a]', '{b}', 'c')}\n"
    )
    assert spec.port_clock == {"a": "clk_a", "b": "clk_a", "c": "clk_a"}
    assert spec.partial_warnings == []


@BUILDERS
def test_built_collection_inside_an_operator(builder: str) -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group clk_a -group "
        f"[remove_from_collection [all_clocks] {_built(builder, 'clk_a', '{vclk}')}]\n"
        + "set_input_delay 1 -clock clk_a "
        f"[remove_from_collection [all_inputs] {_built(builder, 'clk_a', 'clk_b')}]\n"
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b", "clk_g"}]]
    (delay,) = spec.port_delays
    assert list(delay.terms()) == [("*", ("clk_a", "clk_b"))]


def test_nested_lists_flatten() -> None:
    spec = parse(
        "create_clock -name c -period 10 [list [list a [get_ports b]] {c d}]\n"
    )
    assert spec.clocks["c"].ports == ("a", "b", "c", "d")


def test_unrecognised_command_inside_a_list_is_dropped() -> None:
    spec = parse("create_clock -name c -period 10 [list a [foo b]]\n")
    assert spec.clocks["c"].ports == ("a",)
    assert any("not a collection command" in w for w in spec.partial_warnings)


@pytest.mark.parametrize(
    "target",
    ["[get_clocks clk_a]", "[get_cells u/q_reg]", "[all_inputs]"],
    ids=["get_clocks", "get_cells", "all_inputs"],
)
def test_create_clock_non_port_target_is_ignored(target: str) -> None:
    """``_extract_names`` read ``[get_cells u]`` as the ports ``get_cells``
    and ``u``; a target that names no port or pin now selects nothing."""
    spec = parse(f"create_clock -name c -period 10 {target}\n")
    assert spec.clocks["c"].ports == ()
    assert any("is not a port or pin collection" in w for w in spec.partial_warnings)


@pytest.mark.parametrize(
    "target", ["[get_ports {clk[0]}]", "{clk[0]}"], ids=["bracket", "brace"]
)
def test_create_clock_on_a_bus_bit_is_the_whole_port_with_a_warning(
    target: str,
) -> None:
    """``_extract_names`` split ``clk[0]`` into the ports ``clk`` and ``0``.
    A clock is per port (the tracer names a clock root by its port), so a
    bit target is the whole port — said, not silent."""
    spec = parse(f"create_clock -name c -period 10 {target}\n")
    assert spec.clocks["c"].ports == ("clk",)
    (warning,) = _no_line(spec.partial_warnings)
    assert "'clk[0]'" in warning and "whole port" in warning


@needs_tcl
def test_both_backends_read_built_collections_identically() -> None:
    text = (
        "create_clock -name c -period 10 [list [get_ports a] [get_ports b]]\n"
        "create_clock -name d -period 10 [concat [get_ports e] {f g}]\n"
        "set_clock_groups -asynchronous -group [list c] -group [concat [get_clocks d]]\n"
        "set_case_analysis 0 [list [get_ports m] n]\n"
    )
    tcl = parse(text, backend="tcl")
    tok = parse(text, backend="tokenizer")
    assert tcl.clocks == tok.clocks
    assert tcl.async_groups == tok.async_groups == [[{"c"}, {"d"}]]
    assert tcl.case_analysis == tok.case_analysis == {"m": "0", "n": "0"}
    assert tcl.partial_warnings == tok.partial_warnings == []
