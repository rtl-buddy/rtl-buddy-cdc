"""``set_input_delay`` / ``set_output_delay`` diagnostics and port
resolution (rtl-buddy-cdc#357).

Two inputs used to be accepted silently and then have no effect:

1. A ``-clock`` naming a clock no ``create_clock`` /
   ``create_generated_clock`` declares (usually a typo). The parser now
   warns, deciding at the end of the file so a clock declared below the
   delay counts, and a virtual clock (``create_clock -name V`` with no
   port) is a declared clock.
2. Bus-index (``d[3]``) and glob (``d*`` / ``d?``) port targets. The
   parser keeps them as written (:class:`~rtl_buddy_cdc.sdc.PortDelay`);
   :func:`rtl_buddy_cdc.sdc_objects.resolve_port_delays` resolves them
   against the top-level ports with the object-pattern matcher. A bus
   index types the whole port (port typing is per port); a glob selects
   ports of the command's direction that carry no declared clock; a
   pattern matching nothing is warned about.

Every form is covered in bracket / brace / bare spelling on both SDC
backends (the ``sdc_backend`` fixture).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.sdc import ClockSpec, PortDelay, parse


@pytest.fixture(autouse=True)
def _both_backends(sdc_backend: str) -> str:
    return sdc_backend


_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 5 [get_ports clk_b]
"""


def _undeclared(spec: ClockSpec) -> list[str]:
    return [w for w in spec.partial_warnings if "declares clock" in w]


# --- 1. undeclared -clock -----------------------------------------------------


@pytest.mark.parametrize(
    "target", ["[get_ports d]", "{d}", "d"], ids=["bracket", "brace", "bare"]
)
def test_undeclared_delay_clock_warns(target: str) -> None:
    spec = parse(_CLOCKS + f"set_input_delay -clock clk_aa 1 {target}\n")
    (warning,) = _undeclared(spec)
    assert "set_input_delay -clock clk_aa" in warning
    assert "'clk_aa'" in warning and "d" in warning
    # The typing itself is kept, exactly as before.
    assert spec.port_clock["d"] == "clk_aa"


def test_undeclared_output_delay_clock_through_get_clocks() -> None:
    spec = parse(_CLOCKS + "set_output_delay -clock [get_clocks clk_c] 1 {q}\n")
    (warning,) = _undeclared(spec)
    assert warning.startswith("set_output_delay -clock clk_c")


def test_clock_declared_after_the_delay_is_declared() -> None:
    spec = parse(
        "set_input_delay -clock clk_a 1 [get_ports d]\n"
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
    )
    assert _undeclared(spec) == []


def test_virtual_and_generated_clocks_are_declared() -> None:
    spec = parse(
        _CLOCKS
        + "create_clock -name v_ext -period 8\n"
        + "create_generated_clock -name clk_div -source [get_ports clk_a] "
        "-divide_by 2 -master_clock clk_a [get_pins u_div/q]\n"
        + "set_input_delay -clock v_ext 1 [get_ports d]\n"
        + "set_output_delay -clock clk_div 1 [get_ports q]\n"
    )
    assert "v_ext" in spec.clocks and "clk_div" in spec.clocks
    assert _undeclared(spec) == []


def test_delay_without_port_target_is_not_checked() -> None:
    """No port target: nothing is typed, so nothing to report."""
    spec = parse(_CLOCKS + "set_input_delay -clock clk_zz 1\n")
    assert _undeclared(spec) == []
    assert spec.port_delays == []


# --- 2. targets are recorded as written ---------------------------------------


@pytest.mark.parametrize(
    ("target", "written"),
    [
        ("[get_ports {d[3]}]", ("d[3]",)),
        ("{d[3]}", ("d[3]",)),
        ("[get_ports d*]", ("d*",)),
        ("{d* e?}", ("d*", "e?")),
        ("d*", ("d*",)),
        ("e?", ("e?",)),
    ],
    ids=[
        "bit-bracket",
        "bit-brace",
        "glob-bracket",
        "glob-brace",
        "glob-bare",
        "q-bare",
    ],
)
def test_targets_are_recorded_as_written(target: str, written: tuple[str, ...]) -> None:
    spec = parse(_CLOCKS + f"set_input_delay -clock clk_b 1 {target}\n")
    (delay,) = spec.port_delays
    assert delay.command == "set_input_delay"
    assert delay.clock == "clk_b"
    assert delay.targets == written


def test_unevaluated_expr_delay_is_not_a_target() -> None:
    """The tokenizer leaves ``[expr {2 * 0.4}]`` unevaluated; its ``*``
    must not become a glob over every port. The Tcl reader evaluates it
    to a number."""
    spec = parse(_CLOCKS + "set_input_delay -clock clk_a [expr {2 * 0.4}] {d}\n")
    (delay,) = spec.port_delays
    assert delay.targets == ("d",)


def test_pin_collection_is_not_a_port_target() -> None:
    spec = parse(_CLOCKS + "set_input_delay -clock clk_a 1 [get_pins u/a]\n")
    assert spec.port_delays == [] and spec.port_clock == {}


# --- 3. resolution against the netlist ---------------------------------------


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


@pytest.mark.parametrize(
    "target",
    ["[get_ports {d[3]}]", "{d[3]}", "[get_ports {d[1] d[2]}]"],
    ids=["bracket", "brace", "two-bits"],
)
def test_bit_select_types_the_whole_port(tmp_path: Path, target: str) -> None:
    spec, warnings = _resolve(tmp_path, f"set_input_delay -clock clk_b 1 {target}\n")
    assert warnings == []
    assert spec.port_clock == {"d": "clk_b"}


def test_bare_bit_select(tmp_path: Path, sdc_backend: str) -> None:
    """Unbraced ``d[3]`` is Tcl command substitution: the Tcl reader
    runs a command ``3`` (and warns about it) and is left with ``d``;
    the tokenizer keeps ``d[3]``. Both type port ``d``."""
    spec, warnings = _resolve(tmp_path, "set_input_delay -clock clk_b 1 d[3]\n")
    assert warnings == []
    assert spec.port_clock == {"d": "clk_b"}
    unbraced = [w for w in spec.partial_warnings if "unbraced bus index" in w]
    assert len(unbraced) == (1 if sdc_backend == "tcl" else 0)


def test_out_of_range_bit_select_matches_nothing(tmp_path: Path) -> None:
    spec, warnings = _resolve(tmp_path, "set_input_delay -clock clk_b 1 {d[9]}\n")
    (warning,) = warnings
    assert "'d[9]'" in warning and "no top-level port" in warning
    assert "d" not in spec.port_clock


@pytest.mark.parametrize(
    "target", ["[get_ports d*]", "{d*}", "d*"], ids=["bracket", "brace", "bare"]
)
def test_star_glob_types_every_matching_input(tmp_path: Path, target: str) -> None:
    spec, warnings = _resolve(tmp_path, f"set_input_delay -clock clk_a 1 {target}\n")
    assert warnings == []
    assert spec.port_clock == {"d": "clk_a", "dx": "clk_a"}


@pytest.mark.parametrize(
    "target", ["[get_ports e?]", "{e?}", "e?"], ids=["bracket", "brace", "bare"]
)
def test_question_glob(tmp_path: Path, target: str) -> None:
    spec, warnings = _resolve(tmp_path, f"set_input_delay -clock clk_a 1 {target}\n")
    assert warnings == []
    assert spec.port_clock == {"e1": "clk_a"}


def test_glob_selects_the_commands_direction_and_skips_clock_ports(
    tmp_path: Path,
) -> None:
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 [get_ports *]\n"
        "set_output_delay -clock clk_b 1 {q*}\n",
    )
    assert warnings == []
    assert spec.port_clock == {
        "d": "clk_a",
        "dx": "clk_a",
        "e1": "clk_a",
        "q": "clk_b",
        "qq": "clk_b",
    }


@pytest.mark.parametrize(
    "target", ["[get_ports zz*]", "{zz*}", "zz*"], ids=["bracket", "brace", "bare"]
)
def test_glob_matching_nothing_warns(tmp_path: Path, target: str) -> None:
    spec, warnings = _resolve(tmp_path, f"set_input_delay -clock clk_a 1 {target}\n")
    (warning,) = warnings
    assert "'zz*'" in warning and "matches no input port" in warning
    # Kept as written: it types nothing, as before.
    assert spec.port_clock == {"zz*": "clk_a"}


def test_output_glob_naming_only_inputs_warns(tmp_path: Path) -> None:
    _spec, warnings = _resolve(tmp_path, "set_output_delay -clock clk_a 1 {d*}\n")
    (warning,) = warnings
    assert "set_output_delay -clock clk_a" in warning
    assert "matches no output port" in warning


def test_bits_typed_to_different_clocks_warn(tmp_path: Path) -> None:
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 {d[0]}\nset_input_delay -clock clk_b 1 {d[1]}\n",
    )
    (warning,) = warnings
    assert "bits of port 'd'" in warning
    assert "d[0] → clk_a" in warning and "d[1] → clk_b" in warning
    assert spec.port_clock == {"d": "clk_b"}


def test_bit_select_over_a_whole_port_typing_warns(tmp_path: Path) -> None:
    """``d`` on clk_a then ``d[2]`` on clk_b leaves bits on two clocks."""
    _spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 {d}\nset_input_delay -clock clk_b 1 {d[2]}\n",
    )
    assert len(warnings) == 1 and "different clocks" in warnings[0]


def test_whole_port_typing_replaces_earlier_bit_selects(tmp_path: Path) -> None:
    spec, warnings = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 {d[2]}\nset_input_delay -clock clk_b 1 {d}\n",
    )
    assert warnings == []
    assert spec.port_clock == {"d": "clk_b"}


def test_file_order_wins_across_exact_names_and_globs(tmp_path: Path) -> None:
    spec, _ = _resolve(
        tmp_path,
        "set_input_delay -clock clk_a 1 {dx}\n"
        "set_input_delay -clock clk_b 1 {d*}\n"
        "set_input_delay -clock clk_a 1 {d}\n",
    )
    assert spec.port_clock == {"dx": "clk_b", "d": "clk_a"}


def test_exact_names_only_leave_port_clock_untouched(tmp_path: Path) -> None:
    spec = parse(_CLOCKS + "set_input_delay -clock clk_a 1 [get_ports {d dx}]\n")
    before = spec.port_clock
    assert sdc_objects.resolve_port_delays(_module(tmp_path), spec) == []
    assert spec.port_clock is before and before == {"d": "clk_a", "dx": "clk_a"}


def test_hand_built_entries_are_kept(tmp_path: Path) -> None:
    spec = ClockSpec(
        port_clock={"e1": "clk_a", "d*": "clk_b"},
        port_delays=[PortDelay("set_input_delay", "clk_b", ("d*",))],
    )
    assert sdc_objects.resolve_port_delays(_module(tmp_path), spec) == []
    assert spec.port_clock == {"d": "clk_b", "dx": "clk_b", "e1": "clk_a"}


# --- 4. end to end ------------------------------------------------------------

_FIX = Path(__file__).parent / "fixtures" / "bad_unconstrained_input_bus_two_domains"
_NETLIST = _FIX / "bad_unconstrained_input_bus_two_domains.json"
_FIXTURE_SDC = (_FIX / "bad_unconstrained_input_bus_two_domains.sdc").read_text()


def _analyze(tmp_path: Path, extra: str, fmt: str = "json"):
    sdc = tmp_path / "x.sdc"
    sdc.write_text(_FIXTURE_SDC + extra)
    return CliRunner().invoke(
        app, ["analyze", "-n", str(_NETLIST), "-s", str(sdc), "-f", fmt]
    )


def _fired(result) -> Counter[str]:
    return Counter(v["rule_id"] for v in json.loads(result.stdout)["violations"])


@pytest.mark.parametrize(
    "target",
    ["[get_ports i*]", "{i*}", "i?", "[get_ports {in[3]}]", "{in[3]}"],
    ids=["glob-bracket", "glob-brace", "glob-bare", "bit-bracket", "bit-brace"],
)
def test_pattern_typing_reaches_the_rules(tmp_path: Path, target: str) -> None:
    """The fixture's bus ``in`` is untyped, so CDC-011 fires. Typed to
    clk_a through a glob or a bus index, it is a clk_a source instead:
    CDC-011 goes, and the in → q_b path stays a reported crossing."""
    assert _fired(_analyze(tmp_path, ""))["CDC-011"] == 1
    fired = _fired(_analyze(tmp_path, f"set_input_delay -clock clk_a 1 {target}\n"))
    assert fired["CDC-011"] == 0
    assert fired["CDC-004"] >= 1


def test_output_glob_reaches_cdc_024(tmp_path: Path) -> None:
    """``q_*`` on clk_b types both outputs: ``q_b`` (launched on clk_b)
    meets its contract, ``q_a`` (launched on clk_a) does not."""
    result = _analyze(
        tmp_path,
        "set_input_delay -clock clk_a 1 {in}\nset_output_delay -clock clk_b 1 {q_*}\n",
    )
    report = json.loads(result.stdout)
    cdc_024 = [v for v in report["violations"] if v["rule_id"] == "CDC-024"]
    assert len(cdc_024) == 1 and "q_a" in cdc_024[0]["message"]


def test_cli_surfaces_both_warnings(tmp_path: Path) -> None:
    result = _analyze(
        tmp_path,
        "set_input_delay -clock clk_aa 1 {in}\nset_input_delay -clock clk_a 1 {zz*}\n",
        fmt="text",
    )
    warnings = [ln for ln in result.stderr.splitlines() if ln.startswith("warning:")]
    assert any("clock 'clk_aa'" in w for w in warnings), warnings
    assert any("'zz*'" in w for w in warnings), warnings
