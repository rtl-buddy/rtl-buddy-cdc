"""P1 first-class blackbox support (#255).

Covers the rtl-buddy-cdc side of blackbox boundary handling end to end
without requiring a yosys toolchain at test time:

- ``netlist.load`` / ``load_with_blackboxes`` multi-module support: a
  flattened dump carrying blackbox boundary siblings now loads (the
  single-module-after-flatten invariant is relaxed to "one top + N
  blackbox siblings"). Detection keys on ``attributes.blackbox``, not a
  ``$``-prefix rename pass (P1 prep probe).
- the committed ``blackbox_leaf_crossing`` fixture pair: the boundary
  cell loads, is flagged ``is_blackbox``, and the analyzer runs over the
  parent without crashing or hallucinating crossings into the blackbox
  internals.
- the yosys frontend ``--blackboxed-module`` hook + the ``--blackbox``
  guards, driven by monkeypatching ``subprocess.run`` so no real yosys
  is invoked.

Boundary-summary *consumption* (seeding virtual sources at the boundary
so a crossing THROUGH the blackbox is reported) is deferred to P2 per
the #254 data-model decision; P1 only proves the boundary is a
first-class, loadable cell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import cli as cli_mod, netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.flops import find_flops
from rtl_buddy_cdc.frontends import yosys as yosys_fe
from rtl_buddy_cdc.frontends.yosys import YosysError

runner = CliRunner()

FIX_DIR = Path(__file__).parent / "fixtures" / "blackbox_leaf_crossing"
JSON = FIX_DIR / "blackbox_leaf_crossing.json"
SDC = FIX_DIR / "blackbox_leaf_crossing.sdc"


def _write_json(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "design.json"
    p.write_text(json.dumps(data))
    return p


# --------------------------------------------------------------------------
# fixture-level: the committed blackbox boundary pair
# --------------------------------------------------------------------------


def test_fixture_loads_top_and_blackbox_sibling() -> None:
    """The blackbox fixture loads as one top + one blackbox sibling; the
    boundary module is flagged and zero-celled, the parent keeps the
    instance as an ordinary cell of the blackbox type."""
    top, blackboxes = netlist.load_with_blackboxes(JSON)
    assert top.name == "top"
    assert top.is_blackbox is False
    assert top.boundary is None
    assert set(blackboxes) == {"leaf"}
    leaf = blackboxes["leaf"]
    assert leaf.is_blackbox is True
    assert leaf.cells == {}  # boundary cell: zero internals
    # Modelled by port directions only.
    assert leaf.ports["d_in"].direction == "input"
    assert leaf.ports["d_out"].direction == "output"
    # Parent keeps the instance as an ordinary cell typed by module name.
    assert top.cells["u_leaf"].type == "leaf"


def test_fixture_back_compat_load_returns_top_only() -> None:
    """The legacy single-return ``load`` still works on a netlist that
    now contains blackbox siblings — it returns the top and drops the
    boundary modules on the floor."""
    top = netlist.load(JSON)
    assert top.name == "top"
    assert top.is_blackbox is False


def test_fixture_analyzer_runs_over_boundary() -> None:
    """find_flops / assign_domains / find_crossings run on the top with a
    blackbox instance present, without crashing and without inventing
    crossings into the blackbox internals.

    The crossing physically runs src_q (clk_a) -> u_leaf -> dst_q
    (clk_b), but boundary-summary seeding is P2; today the boundary is
    opaque so no register-to-register crossing is reported through it."""
    top = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, top)

    flops = find_flops(top)
    assert {f.name for f in flops} == {"$driver$src_q", "$driver$dst_q"}

    domains = {fd.flop.name: fd.clock for fd in assign_domains(top)}
    assert domains["$driver$src_q"] == "clk_a"
    assert domains["$driver$dst_q"] == "clk_b"

    crossings = find_crossings(
        top,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    # No flop-to-flop crossing leaks through the opaque boundary; the
    # typed d_in port is fully synchronous (clk_a -> src_q), so the run
    # is clean. P2 will seed the boundary and surface the real crossing.
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    assert async_crossings == []


# --------------------------------------------------------------------------
# netlist.load_with_blackboxes — branch coverage on tiny hand-built JSON
# --------------------------------------------------------------------------


def test_blackbox_attribute_detection(tmp_path: Path) -> None:
    """A module with a truthy ``attributes.blackbox`` bit-string is a
    boundary sibling; the single non-blackbox non-``$`` module is the
    top."""
    data = {
        "modules": {
            "leaf": {
                "attributes": {"blackbox": "00000000000000000000000000000001"},
                "ports": {"d": {"direction": "input", "bits": [2]}},
                "cells": {},
                "netnames": {},
            },
            "top": {
                "ports": {"clk": {"direction": "input", "bits": [3]}},
                "cells": {},
                "netnames": {},
            },
        }
    }
    top, blackboxes = netlist.load_with_blackboxes(_write_json(tmp_path, data))
    assert top.name == "top"
    assert list(blackboxes) == ["leaf"]
    assert blackboxes["leaf"].is_blackbox is True


def test_blackbox_zero_attribute_is_not_blackbox(tmp_path: Path) -> None:
    """An all-zero ``blackbox`` bit-string means the attribute is present
    but unset — the module is a normal module, so two of them is still
    the ambiguous-top error."""
    data = {
        "modules": {
            "alpha": {
                "attributes": {"blackbox": "00000000000000000000000000000000"},
                "ports": {},
                "cells": {},
                "netnames": {},
            },
            "beta": {"ports": {}, "cells": {}, "netnames": {}},
        }
    }
    with pytest.raises(ValueError, match="expected exactly one user module"):
        netlist.load_with_blackboxes(_write_json(tmp_path, data))


def test_blackbox_truthy_short_form(tmp_path: Path) -> None:
    """A non-bit-string truthy blackbox value (defensive ``"1"``) is
    accepted as a boundary too."""
    data = {
        "modules": {
            "leaf": {
                "attributes": {"blackbox": "1"},
                "ports": {},
                "cells": {},
                "netnames": {},
            },
            "top": {"ports": {}, "cells": {}, "netnames": {}},
        }
    }
    _top, blackboxes = netlist.load_with_blackboxes(_write_json(tmp_path, data))
    assert list(blackboxes) == ["leaf"]


def test_dollar_prefixed_stub_still_discarded(tmp_path: Path) -> None:
    """The legacy ``$``-prefixed paramod/stub convention is still
    discarded (not treated as a blackbox), so a top + a ``$`` stub keeps
    loading the top."""
    data = {
        "modules": {
            "$paramod$abc\\stub": {"ports": {}, "cells": {}, "netnames": {}},
            "top": {
                "ports": {"clk": {"direction": "input", "bits": [2]}},
                "cells": {},
                "netnames": {},
            },
        }
    }
    top, blackboxes = netlist.load_with_blackboxes(_write_json(tmp_path, data))
    assert top.name == "top"
    assert blackboxes == {}


def test_multiple_blackbox_siblings(tmp_path: Path) -> None:
    """Zero-or-more blackbox siblings load alongside the single top."""
    data = {
        "modules": {
            "leaf_a": {
                "attributes": {"blackbox": "00000000000000000000000000000001"},
                "ports": {},
                "cells": {},
                "netnames": {},
            },
            "leaf_b": {
                "attributes": {"blackbox": "00000000000000000000000000000001"},
                "ports": {},
                "cells": {},
                "netnames": {},
            },
            "top": {"ports": {}, "cells": {}, "netnames": {}},
        }
    }
    top, blackboxes = netlist.load_with_blackboxes(_write_json(tmp_path, data))
    assert top.name == "top"
    assert set(blackboxes) == {"leaf_a", "leaf_b"}


def test_ambiguous_non_blackbox_modules_still_raise(tmp_path: Path) -> None:
    """Two non-``$`` non-blackbox modules remain ambiguous — the relaxed
    invariant only exempts blackbox siblings."""
    data = {
        "modules": {
            "alpha": {"ports": {}, "cells": {}, "netnames": {}},
            "beta": {"ports": {}, "cells": {}, "netnames": {}},
        }
    }
    with pytest.raises(ValueError, match="expected exactly one user module"):
        netlist.load_with_blackboxes(_write_json(tmp_path, data))


def test_all_blackbox_no_top_raises(tmp_path: Path) -> None:
    """A dump with only blackbox modules and no real top is unusable —
    zero top candidates raises."""
    data = {
        "modules": {
            "leaf": {
                "attributes": {"blackbox": "00000000000000000000000000000001"},
                "ports": {},
                "cells": {},
                "netnames": {},
            },
        }
    }
    with pytest.raises(ValueError, match="expected exactly one user module"):
        netlist.load_with_blackboxes(_write_json(tmp_path, data))


def test_empty_modules_still_raises(tmp_path: Path) -> None:
    """The no-modules guard is unchanged."""
    with pytest.raises(ValueError, match="no modules in JSON"):
        netlist.load_with_blackboxes(_write_json(tmp_path, {"modules": {}}))


# --------------------------------------------------------------------------
# yosys frontend --blackboxed-module hook + --blackbox guards
# --------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_frontend_threads_blackboxed_module(tmp_path, monkeypatch) -> None:
    """``blackbox=[...]`` becomes one ``--blackboxed-module`` flag per
    module on the ``read_slang`` line, and the JSON the (faked) yosys
    'wrote' is loaded back."""
    plugin = tmp_path / "slang.so"
    plugin.write_text("")  # existence check only

    captured: dict[str, str] = {}

    payload = {
        "modules": {
            "leaf": {
                "attributes": {"blackbox": "00000000000000000000000000000001"},
                "ports": {},
                "cells": {},
                "netnames": {},
            },
            "top": {"ports": {}, "cells": {}, "netnames": {}},
        }
    }

    def fake_run(cmd, capture_output, text):  # noqa: ARG001
        script = cmd[-1]
        captured["script"] = script
        # The script ends with `write_json <path>`; honour it so load
        # succeeds.
        out_path = script.rsplit("write_json ", 1)[1].strip().strip("'\"")
        Path(out_path).write_text(json.dumps(payload))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(yosys_fe.shutil, "which", lambda _name: "/usr/bin/yosys")
    monkeypatch.setattr(yosys_fe.Path, "exists", lambda self: True)
    monkeypatch.setattr(yosys_fe.subprocess, "run", fake_run)

    module = yosys_fe.elaborate(
        [tmp_path / "leaf.sv", tmp_path / "top.sv"],
        "top",
        plugin_path=str(plugin),
        blackbox=["leaf", "other"],
    )
    assert module.name == "top"
    assert "--blackboxed-module leaf" in captured["script"]
    assert "--blackboxed-module other" in captured["script"]
    # Must ride on the read_slang line, before the sources.
    assert "read_slang" in captured["script"]


def test_blackbox_without_plugin_errors(tmp_path, monkeypatch) -> None:
    """``--blackbox`` is a read_slang feature; requesting it on the
    built-in read_verilog frontend (no plugin) is a hard error, not a
    silent no-op."""
    monkeypatch.setattr(yosys_fe.shutil, "which", lambda _name: "/usr/bin/yosys")
    monkeypatch.setattr(yosys_fe.Path, "exists", lambda self: True)

    with pytest.raises(YosysError, match="--blackbox requires the yosys-slang plugin"):
        yosys_fe.elaborate(
            [tmp_path / "top.sv"],
            "top",
            plugin_path=None,
            blackbox=["leaf"],
        )


def test_no_blackbox_omits_flag(tmp_path, monkeypatch) -> None:
    """With no blackbox modules the read_slang line is unchanged (no
    stray ``--blackboxed-module``)."""
    plugin = tmp_path / "slang.so"
    plugin.write_text("")
    captured: dict[str, str] = {}

    def fake_run(cmd, capture_output, text):  # noqa: ARG001
        script = cmd[-1]
        captured["script"] = script
        out_path = script.rsplit("write_json ", 1)[1].strip().strip("'\"")
        Path(out_path).write_text(
            json.dumps({"modules": {"top": {"ports": {}, "cells": {}, "netnames": {}}}})
        )
        return _FakeProc(returncode=0)

    monkeypatch.setattr(yosys_fe.shutil, "which", lambda _name: "/usr/bin/yosys")
    monkeypatch.setattr(yosys_fe.Path, "exists", lambda self: True)
    monkeypatch.setattr(yosys_fe.subprocess, "run", fake_run)

    yosys_fe.elaborate(
        [tmp_path / "top.sv"], "top", plugin_path=str(plugin), blackbox=[]
    )
    assert "--blackboxed-module" not in captured["script"]


# --------------------------------------------------------------------------
# CLI surface: repeatable --blackbox on `lint` forwards into elaborate
# --------------------------------------------------------------------------


def test_lint_blackbox_flag_forwarded(tmp_path: Path, monkeypatch) -> None:
    """A repeatable ``--blackbox`` on ``lint`` is collected and passed
    through to ``elaborate`` as ``blackbox=[...]``."""
    sv = tmp_path / "top.sv"
    sv.write_text("module top(); endmodule\n")
    sdc = tmp_path / "top.sdc"
    sdc.write_text("create_clock -name c -period 1 [get_ports c]\n")

    captured: dict[str, object] = {}

    def fake_elaborate(sources, top, **kwargs):  # noqa: ARG001
        captured["blackbox"] = kwargs.get("blackbox")
        return netlist.load(JSON)

    monkeypatch.setattr(cli_mod, "elaborate", fake_elaborate)

    result = runner.invoke(
        app,
        [
            "lint",
            str(sv),
            "--top",
            "top",
            "--frontend",
            "yosys",
            "--blackbox",
            "leaf",
            "--blackbox",
            "other",
            "-s",
            str(sdc),
        ],
    )
    assert result.exit_code in (0, 1), result.output
    assert captured["blackbox"] == ["leaf", "other"]
