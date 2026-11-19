"""The ``lint`` Yosys flow folds ``$buf`` cells into wires (rtl-buddy-cdc#414).

``read_slang`` writes a ``$buf`` per continuous assignment and port
connection. ``opt_clean`` used to remove them; since the flow dropped
``opt_clean`` (so that an unobserved register is named, not silently
deleted) the frontend does that one rewrite itself. Left in, a ``$buf``
between a source flop and a synchroniser's first stage is counted as
combinational logic: a false CDC-003 on every synchroniser instance.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontends.yosys import _collapse_buffers

runner = CliRunner()

_FIXTURE = Path(__file__).parent / "fixtures" / "ip_cdc_handshake"
_PLUGIN = os.environ.get("RTL_BUDDY_SLANG_PLUGIN")
_HAVE_PLUGIN = bool(_PLUGIN) and Path(str(_PLUGIN)).exists()
_HAVE_YOSYS = shutil.which("yosys") is not None


def _buf(a: list[object], y: list[object], **attrs: object) -> dict[str, Any]:
    return {
        "type": "$buf",
        "attributes": dict(attrs),
        "connections": {"A": a, "Y": y},
    }


def _netlist(cells: dict[str, Any], ports: dict[str, Any]) -> dict[str, Any]:
    return {"modules": {"top": {"ports": ports, "cells": cells, "netnames": {}}}}


def _fold(tmp_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "n.json"
    path.write_text(json.dumps(data))
    _collapse_buffers(path)
    module = json.loads(path.read_text())["modules"]["top"]
    assert isinstance(module, dict)
    return module


def test_a_buffer_chain_becomes_a_wire(tmp_path: Path) -> None:
    """Two buffers in series between a flop's ``Q`` and the next ``D``:
    both go, and the reader sees the original bit."""
    data = _netlist(
        {
            "src": {"type": "$dff", "connections": {"CLK": [2], "D": [3], "Q": [4]}},
            "b0": _buf([4], [5]),
            "b1": _buf([5], [6]),
            "dst": {"type": "$dff", "connections": {"CLK": [7], "D": [6], "Q": [8]}},
        },
        {
            "ck_a": {"direction": "input", "bits": [2]},
            "d": {"direction": "input", "bits": [3]},
            "ck_b": {"direction": "input", "bits": [7]},
            "q": {"direction": "output", "bits": [8]},
        },
    )
    data["modules"]["top"]["netnames"] = {"mid": {"bits": [5]}}
    module = _fold(tmp_path, data)
    assert sorted(module["cells"]) == ["dst", "src"]
    assert module["cells"]["dst"]["connections"]["D"] == [4]
    assert module["netnames"]["mid"]["bits"] == [4]


def test_an_output_port_takes_the_driver_bit(tmp_path: Path) -> None:
    """A buffer driving an output port: the port now names the driver's
    bit (or the constant), as it does after ``opt_clean``."""
    module = _fold(
        tmp_path,
        _netlist(
            {"b0": _buf([2, "1"], [3, 4])},
            {
                "i": {"direction": "input", "bits": [2]},
                "o": {"direction": "output", "bits": [3, 4]},
            },
        ),
    )
    assert module["cells"] == {}
    assert module["ports"]["o"]["bits"] == [2, "1"]


def test_buffers_that_stay(tmp_path: Path) -> None:
    """``keep``, a width mismatch, a ``Y`` on an input port bit, and the
    mapped ``$_BUF_`` gate are all left alone; the file is not rewritten."""
    data = _netlist(
        {
            "kept": _buf([2], [5], keep="00000000000000000000000000000001"),
            "odd": _buf([2], [6, 7]),
            "into_input": _buf([5], [3]),
            "gate": {"type": "$_BUF_", "connections": {"A": [2], "Y": [8]}},
        },
        {
            "i": {"direction": "input", "bits": [2]},
            "j": {"direction": "input", "bits": [3]},
        },
    )
    path = tmp_path / "n.json"
    text = json.dumps(data)
    path.write_text(text)
    _collapse_buffers(path)
    assert path.read_text() == text


def test_a_buffer_loop_terminates(tmp_path: Path) -> None:
    """Two buffers feeding each other fold without hanging."""
    module = _fold(
        tmp_path,
        _netlist({"b0": _buf([3], [2]), "b1": _buf([2], [3])}, {}),
    )
    assert module["cells"] == {}


@pytest.mark.yosys_slang
@pytest.mark.skipif(
    not (_HAVE_YOSYS and _HAVE_PLUGIN),
    reason="yosys or RTL_BUDDY_SLANG_PLUGIN missing",
)
def test_read_slang_handshake_is_clean(tmp_path: Path) -> None:
    """The handshake IP through ``read_slang``: both synchronisers are
    recognised, no CDC-003 from a port-connection ``$buf``."""
    out = tmp_path / "o.json"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "ip_cdc_handshake",
            "--sdc",
            str(_FIXTURE / "ip_cdc_handshake.sdc"),
            "--frontend",
            "yosys",
            "--yosys-plugin",
            str(_PLUGIN),
            "--format",
            "json",
            "--output",
            str(out),
            str(_FIXTURE / "ip_cdc_sync.sv"),
            str(_FIXTURE / "ip_cdc_handshake.sv"),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert report["violations"] == []
    assert len(report["crossings"]) == 3
