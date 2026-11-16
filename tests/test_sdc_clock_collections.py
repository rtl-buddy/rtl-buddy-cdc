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

import re

import pytest

from rtl_buddy_cdc import sdc as sdc_mod
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
