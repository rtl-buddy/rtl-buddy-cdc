"""Collection operators and unrecognised collection commands in SDC
targets (rtl-buddy-cdc#382).

On the Tcl backend the worker's ``unknown`` handler used to hand back the
*last argument* of any command it did not recognise, so

    set_input_delay 1 -clock clk_a [remove_from_collection [all_inputs] [get_ports clk]]

typed ``clk`` — the port the author meant to remove — while the tokenizer
backend typed nothing. Now:

- ``remove_from_collection`` / ``add_to_collection`` over port collections
  are evaluated for ``set_input_delay`` / ``set_output_delay`` targets on
  both backends. The parser records the removed patterns on the
  :class:`~rtl_buddy_cdc.sdc.PortDelay` (``excludes``) and
  :func:`rtl_buddy_cdc.sdc_objects.resolve_port_delays` removes them once
  the netlist is known (``[all_inputs]`` is the glob ``*`` until then).
- Any other unrecognised command used as a collection resolves to nothing,
  with a ``partial_warnings`` entry, on both backends and in every
  supported command — never to one of its arguments.

Every operand form is covered: bracket (``[get_ports x]``), brace
(``{x}``) and bare (``x``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.sdc import ClockSpec, PortDelay, parse

needs_tcl = pytest.mark.skipif(
    not sdc_mod.tcl_available(), reason="_tkinter unavailable: no Tcl backend"
)

_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 5 [get_ports clk_b]
"""


def _unrecognised(spec: ClockSpec) -> list[str]:
    return [w for w in spec.partial_warnings if "not a collection command" in w]


def _module(tmp_path: Path) -> netlist.Module:
    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "clk_b": {"direction": "input", "bits": [3]},
        "d": {"direction": "input", "bits": [4, 5, 6, 7]},
        "dx": {"direction": "input", "bits": [8]},
        "e1": {"direction": "input", "bits": [9]},
        "q": {"direction": "output", "bits": [10, 11]},
        "qq": {"direction": "output", "bits": [12]},
    }
    netnames = {
        name: {"bits": p["bits"], "hide_name": 0, "attributes": {}}
        for name, p in ports.items()
    }
    raw = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": {},
                "netnames": netnames,
            }
        }
    }
    path = tmp_path / "top.json"
    path.write_text(json.dumps(raw))
    return netlist.load(path)


def _resolve(tmp_path: Path, body: str) -> tuple[ClockSpec, list[str]]:
    spec = parse(_CLOCKS + body)
    return spec, sdc_objects.resolve_port_delays(_module(tmp_path), spec)


# --- 1. the reported bug: the removed port must not be typed ------------------


@pytest.mark.parametrize(
    "removed",
    ["[get_ports clk]", "{clk}", "clk"],
    ids=["bracket", "brace", "bare"],
)
def test_remove_from_all_inputs_does_not_type_the_removed_port(
    sdc_backend: str, removed: str
) -> None:
    spec = parse(
        _CLOCKS
        + f"set_input_delay 1 -clock clk_a [remove_from_collection [all_inputs] {removed}]\n"
    )
    assert "clk" not in spec.port_clock
    assert spec.port_delays == [
        PortDelay("set_input_delay", "clk_a", ("*",), excludes=(("clk",),))
    ]
    assert spec.partial_warnings == []


@needs_tcl
def test_both_backends_read_collection_operators_identically() -> None:
    text = (
        _CLOCKS + "set_input_delay 1 -clock clk_a [remove_from_collection [all_inputs] "
        "[get_ports {clk_a clk_b}]]\n"
        + "set_input_delay 1 -clock clk_b [add_to_collection [get_ports d] {dx e1}]\n"
        + "set_output_delay 1 -clock clk_b [remove_from_collection [all_outputs] qq]\n"
        + "set_input_delay 1 -clock clk_b [filter_collection [all_inputs] {name =~ d*}]\n"
    )
    tcl = parse(text, backend="tcl")
    tok = parse(text, backend="tokenizer")
    assert tcl.port_delays == tok.port_delays
    assert tcl.port_clock == tok.port_clock
    # Only the Tcl backend knows source lines.
    no_line = [re.sub(r" \(line \d+\)", "", w) for w in tcl.partial_warnings]
    assert no_line == tok.partial_warnings
    assert len(tok.partial_warnings) == 1


def test_remove_from_explicit_list_drops_the_name_at_parse_time(
    sdc_backend: str,
) -> None:
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock clk_a "
        "[remove_from_collection [get_ports {a b c}] [get_ports b]]\n"
    )
    assert spec.port_clock == {"a": "clk_a", "c": "clk_a"}


@pytest.mark.parametrize(
    "added",
    ["[get_ports {b c}]", "{b c}", "b"],
    ids=["bracket", "brace", "bare"],
)
def test_add_to_collection_types_both_operands(sdc_backend: str, added: str) -> None:
    spec = parse(
        _CLOCKS
        + f"set_input_delay 1 -clock clk_a [add_to_collection [get_ports a] {added}]\n"
    )
    expected = {"a", "b"} | ({"c"} if "c" in added else set())
    assert set(spec.port_clock) == expected
    assert spec.partial_warnings == []


def test_add_to_collection_unique_flag_is_accepted(sdc_backend: str) -> None:
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock clk_a "
        "[add_to_collection -unique [get_ports a] [get_ports a]]\n"
    )
    assert spec.port_clock == {"a": "clk_a"}
    assert spec.partial_warnings == []


def test_nested_operators(sdc_backend: str) -> None:
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock clk_a [remove_from_collection "
        "[add_to_collection [get_ports {a b}] [get_ports c]] [get_ports b]]\n"
    )
    assert spec.port_clock == {"a": "clk_a", "c": "clk_a"}
    (delay,) = spec.port_delays
    assert delay.targets == ("a", "b", "c")


def test_union_of_a_difference_keeps_each_term(sdc_backend: str) -> None:
    """``(all inputs − clk) ∪ x``: the removal applies to its own term."""
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock clk_a [add_to_collection "
        "[remove_from_collection [all_inputs] [get_ports x]] [get_ports x]]\n"
    )
    (delay,) = spec.port_delays
    assert list(delay.terms()) == [("*", ("x",)), ("x", ())]
    assert spec.port_clock["x"] == "clk_a"


def test_output_delay_removal(sdc_backend: str) -> None:
    spec = parse(
        _CLOCKS + "set_output_delay 1 -clock clk_b "
        "[remove_from_collection [all_outputs] [get_ports qq]]\n"
    )
    assert spec.port_delays == [
        PortDelay("set_output_delay", "clk_b", ("*",), excludes=(("qq",),))
    ]


@pytest.mark.parametrize(
    ("operator", "why"),
    [
        ("remove_from_collection -intersect [all_inputs] [get_ports a]", "-intersect"),
        ("remove_from_collection [all_inputs]", "two operands"),
        (
            "remove_from_collection [all_inputs] "
            "[remove_from_collection [all_inputs] [get_ports a]]",
            "removal inside the removed",
        ),
        ("remove_from_collection [get_pins u/a] [get_ports a]", "not a port"),
    ],
    ids=["intersect", "arity", "nested-removal", "pin-operand"],
)
def test_unsupported_operator_forms_type_nothing_and_warn(
    sdc_backend: str, operator: str, why: str
) -> None:
    spec = parse(_CLOCKS + f"set_input_delay 1 -clock clk_a [{operator}]\n")
    assert spec.port_delays == [] and spec.port_clock == {}
    assert any(why in w for w in spec.partial_warnings), spec.partial_warnings


@needs_tcl
def test_operator_through_a_tcl_variable() -> None:
    spec = parse(
        _CLOCKS
        + "set ins [remove_from_collection [all_inputs] [get_ports clk_a]]\n"
        + "set_input_delay 1 -clock clk_a $ins\n",
        backend="tcl",
    )
    assert spec.port_delays == [
        PortDelay("set_input_delay", "clk_a", ("*",), excludes=(("clk_a",),))
    ]


# --- 2. resolution against the netlist ----------------------------------------


@pytest.mark.parametrize(
    "removed",
    ["[get_ports dx]", "{dx}", "dx"],
    ids=["bracket", "brace", "bare"],
)
def test_removed_port_stays_untyped_after_resolution(
    tmp_path: Path, sdc_backend: str, removed: str
) -> None:
    spec, warnings = _resolve(
        tmp_path,
        f"set_input_delay -clock clk_a 1 [remove_from_collection [all_inputs] {removed}]\n",
    )
    assert warnings == []
    assert spec.port_clock == {"d": "clk_a", "e1": "clk_a"}


def test_removed_glob(tmp_path: Path, sdc_backend: str) -> None:
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 [remove_from_collection [all_inputs] {d*}]\n",
    )
    assert warnings == []
    assert spec.port_clock == {"e1": "clk_a"}


def test_removing_some_bits_keeps_the_port(tmp_path: Path, sdc_backend: str) -> None:
    """Port typing is per port: the bits left over still type ``d``."""
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 [remove_from_collection [get_ports d] {d[0]}]\n",
    )
    assert warnings == []
    assert spec.port_clock == {"d": "clk_a"}


def test_removing_every_bit_drops_the_port(tmp_path: Path, sdc_backend: str) -> None:
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 "
        "[remove_from_collection [get_ports {d e1}] {d[*]}]\n",
    )
    assert warnings == []
    assert spec.port_clock == {"e1": "clk_a"}


def test_removed_pattern_matching_nothing_warns(
    tmp_path: Path, sdc_backend: str
) -> None:
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 [remove_from_collection [all_inputs] {nope}]\n",
    )
    (warning,) = warnings
    assert "'nope'" in warning and "removes nothing" in warning
    assert spec.port_clock == {"d": "clk_a", "dx": "clk_a", "e1": "clk_a"}


def test_hand_built_delay_with_excludes(tmp_path: Path) -> None:
    spec = ClockSpec(
        port_delays=[
            PortDelay(
                "set_input_delay", "clk_b", ("*", "dx"), excludes=(("d*", "clk*"), ())
            )
        ]
    )
    assert sdc_objects.resolve_port_delays(_module(tmp_path), spec) == []
    assert spec.port_clock == {"dx": "clk_b", "e1": "clk_b"}


def test_terms_pad_missing_excludes() -> None:
    delay = PortDelay("set_input_delay", "c", ("a", "b"))
    assert list(delay.terms()) == [("a", ()), ("b", ())]


# --- 3. unrecognised collection commands resolve to nothing -------------------


@pytest.mark.parametrize(
    "target",
    [
        "[foo [get_ports a]]",
        "[filter_collection [all_inputs] {name =~ d*}]",
        "[sort_collection [get_ports {a b}] name]",
        "[index_collection [get_ports {a b}] 0]",
        "[get_object_name [get_ports a]]",
        "[get_ports [foo a]]",
    ],
    ids=["foo", "filter", "sort", "index", "object-name", "nested-in-get"],
)
def test_unrecognised_delay_target_types_nothing(sdc_backend: str, target: str) -> None:
    spec = parse(_CLOCKS + f"set_input_delay 1 -clock clk_a {target}\n")
    assert spec.port_clock == {} and spec.port_delays == []
    (warning,) = _unrecognised(spec)
    assert warning.startswith("set_input_delay")


def test_unrecognised_item_is_dropped_from_a_list(sdc_backend: str) -> None:
    spec = parse(_CLOCKS + "set_input_delay 1 -clock clk_a {a [foo b]}\n")
    assert spec.port_clock == {"a": "clk_a"}
    assert len(_unrecognised(spec)) == 1


def test_unrecognised_clock_operand(sdc_backend: str) -> None:
    spec = parse(_CLOCKS + "set_input_delay 1 -clock [foo clk_a] [get_ports d]\n")
    assert spec.port_clock == {} and spec.port_delays == []
    assert len(_unrecognised(spec)) == 1
    assert any("no -clock anchor" in w for w in spec.partial_warnings)


def test_collection_operator_is_unrecognised_outside_delay_targets(
    sdc_backend: str,
) -> None:
    spec = parse(
        _CLOCKS + "set_case_analysis 0 [remove_from_collection [all_inputs] {a}]\n"
    )
    assert spec.case_analysis == {}
    assert len(_unrecognised(spec)) == 1


def test_unrecognised_create_clock_target(sdc_backend: str) -> None:
    spec = parse("create_clock -name c -period 10 [foo [get_ports clk]]\n")
    assert spec.clocks["c"].ports == ()
    assert len(_unrecognised(spec)) == 1


def test_unrecognised_false_path_endpoint(sdc_backend: str) -> None:
    spec = parse(
        _CLOCKS
        + "set_false_path -from [foo [get_clocks clk_a]] -to [get_clocks clk_b]\n"
    )
    assert spec.false_path_pairs == set()
    assert len(_unrecognised(spec)) == 1


def test_clock_group_operator_is_evaluated(sdc_backend: str) -> None:
    """Dropped with a warning before rtl-buddy-cdc#390; now evaluated over
    clocks (``tests/test_sdc_clock_collections.py`` has the rest)."""
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group [get_clocks clk_a] "
        "-group [remove_from_collection [all_clocks] [get_clocks clk_a]]\n"
    )
    assert spec.async_groups == [[{"clk_a"}, {"clk_b"}]]
    assert spec.partial_warnings == []


def test_unrecognised_clock_group(sdc_backend: str) -> None:
    spec = parse(
        _CLOCKS + "set_clock_groups -asynchronous -group [get_clocks clk_a] "
        "-group [filter_collection [all_clocks] {name != clk_a}]\n"
    )
    assert spec.async_groups == []
    assert len(_unrecognised(spec)) == 1


def test_list_builder_is_expanded_by_the_tokenizer() -> None:
    """The Tcl backend evaluates ``list``; since rtl-buddy-cdc#390 the
    tokenizer expands it to the same items (it used to report it as an
    unrecognised command and type nothing)."""
    text = _CLOCKS + "set_input_delay 1 -clock clk_a [list a b]\n"
    tok = parse(text, backend="tokenizer")
    assert tok.port_clock == {"a": "clk_a", "b": "clk_a"}
    assert _unrecognised(tok) == []


def test_unevaluated_tcl_builtin_is_reported_by_the_tokenizer() -> None:
    """Any other Tcl builtin the tokenizer does not evaluate is reported."""
    text = _CLOCKS + "set_input_delay 1 -clock clk_a [lsort {b a}]\n"
    tok = parse(text, backend="tokenizer")
    assert tok.port_clock == {}
    assert len(_unrecognised(tok)) == 1


@needs_tcl
def test_tcl_list_of_collections_is_split_per_item() -> None:
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock clk_a [list [get_ports a] [get_ports b]]\n",
        backend="tcl",
    )
    assert spec.port_clock == {"a": "clk_a", "b": "clk_a"}


def test_bus_index_is_not_a_command(sdc_backend: str) -> None:
    spec = parse(_CLOCKS + "set_input_delay 1 -clock clk_a [get_ports {d[3] e[*]}]\n")
    assert _unrecognised(spec) == []


def test_unevaluated_variable_index_is_not_a_command() -> None:
    """The tokenizer leaves ``d[$i]`` as written; ``$i`` is no command."""
    spec = parse(
        _CLOCKS + "set_input_delay 1 -clock clk_a [get_ports {d[$i]}]\n",
        backend="tokenizer",
    )
    assert _unrecognised(spec) == []
    assert spec.port_clock == {"d[$i]": "clk_a"}


def test_foreach_in_collection_body_is_reported(sdc_backend: str) -> None:
    spec = parse(
        _CLOCKS
        + "foreach_in_collection p [get_ports d*] {\n"
        + "    set_input_delay 1 -clock clk_a $p\n"
        + "}\n"
    )
    assert spec.port_delays == []
    assert any(w.startswith("foreach_in_collection") for w in spec.partial_warnings)


# --- 4. end to end ------------------------------------------------------------

_FIX = Path(__file__).parent / "fixtures" / "bad_unconstrained_input_bus_two_domains"
_NETLIST = _FIX / "bad_unconstrained_input_bus_two_domains.json"
_FIXTURE_SDC = (_FIX / "bad_unconstrained_input_bus_two_domains.sdc").read_text()


def _fired(tmp_path: Path, extra: str) -> Counter[str]:
    sdc = tmp_path / "x.sdc"
    sdc.write_text(_FIXTURE_SDC + extra)
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(_NETLIST), "-s", str(sdc), "-f", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return Counter(v["rule_id"] for v in json.loads(result.stdout)["violations"])


@pytest.mark.parametrize(
    ("operator", "cdc_011"),
    [
        ("[remove_from_collection [all_inputs] [get_ports in]]", 1),
        ("[remove_from_collection [all_inputs] {in}]", 1),
        ("[remove_from_collection [all_inputs] in]", 1),
        ("[remove_from_collection [all_inputs] [get_ports clk_a]]", 0),
        ("[add_to_collection [get_ports clk_a] [get_ports in]]", 0),
    ],
    ids=["remove-bracket", "remove-brace", "remove-bare", "remove-clock", "add"],
)
def test_operators_reach_the_rules(
    tmp_path: Path, sdc_backend: str, operator: str, cdc_011: int
) -> None:
    """The fixture's bus ``in`` is untyped, so CDC-011 fires. Removing it
    from ``[all_inputs]`` must leave it untyped; removing the clock port
    types it."""
    fired = _fired(tmp_path, f"set_input_delay -clock clk_a 1 {operator}\n")
    assert fired["CDC-011"] == cdc_011
