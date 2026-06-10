"""Path-arg resolution tests for the CLI (issue #245).

The downstream ``rtl_buddy`` driver runs the tool with ``cwd`` set to a
deeply-nested artefact dir and forwards a config's path-bearing args
(``--yosys-plugin`` / ``--emit-*``) verbatim. Resolving those relative to
``Path.cwd()`` made them shift by the artefact-dir depth and silently
broke. These tests pin the durable behaviour:

  * relative ``--emit-*`` / ``--yosys-plugin`` args anchor to
    ``--project-root`` (or the ``--sdc`` dir, or cwd) — *not* the process
    cwd;
  * an ``--emit-*`` target's parent dir is created (mkdir -p) before the
    write, so an uncommitted dir like ``.rtl-buddy/overlays/`` doesn't
    raise FileNotFoundError;
  * absolute path args are untouched.

The ``analyze`` path consumes a committed Yosys-JSON fixture; the
``lint`` plugin-anchoring test patches ``elaborate`` to capture the
resolved string without needing a real yosys/plugin.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

import rtl_buddy_cdc.cli as cli_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontends.yosys import YosysError

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures"
_BAD_DIR = FIX / "bad_single_ff_sync"
_BAD_JSON = _BAD_DIR / "bad_single_ff_sync.json"
_BAD_SDC = _BAD_DIR / "bad_single_ff_sync.sdc"


def _skip_if_missing(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            pytest.skip(f"fixture not built: {p}")


def _emit(args: list[str], cwd: Path, monkeypatch) -> Result:
    """Invoke ``analyze --emit-domain-map ... --no-findings`` from ``cwd``.

    Anchoring is cwd-sensitive, so the test must control cwd explicitly
    rather than inherit pytest's.
    """
    monkeypatch.chdir(cwd)
    return runner.invoke(
        app,
        ["analyze", "-n", str(_BAD_JSON), "-s", str(_BAD_SDC), *args, "--no-findings"],
    )


def test_relative_emit_anchored_to_project_root(tmp_path, monkeypatch) -> None:
    """A relative ``--emit-domain-map`` resolves under ``--project-root``,
    not the (different) cwd the tool was launched in."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    root = tmp_path / "root"
    cwd = tmp_path / "deep" / "artefacts" / "name"
    root.mkdir()
    cwd.mkdir(parents=True)

    result = _emit(
        ["--project-root", str(root), "--emit-domain-map", "out/dm.json"],
        cwd,
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert (root / "out" / "dm.json").exists()
    # And explicitly NOT resolved against cwd.
    assert not (cwd / "out" / "dm.json").exists()


def test_relative_emit_creates_missing_parent_dir(tmp_path, monkeypatch) -> None:
    """The emit target's parent is created (mkdir -p) — mirrors writing
    into an uncommitted ``.rtl-buddy/overlays/`` on a fresh checkout."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    root = tmp_path / "root"
    root.mkdir()
    nested = "a/b/c/clock-map.json"
    assert not (root / "a").exists()

    result = _emit(
        ["--project-root", str(root), "--emit-domain-map", nested],
        tmp_path,
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert (root / nested).exists()


def test_relative_emit_anchored_to_sdc_dir_without_project_root(
    tmp_path, monkeypatch
) -> None:
    """With no ``--project-root``, a relative emit anchors to the dir of
    ``--sdc`` (the config the tool is invoked against)."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    sdc_dir = tmp_path / "cfg"
    cwd = tmp_path / "elsewhere"
    sdc_dir.mkdir()
    cwd.mkdir()
    sdc_copy = sdc_dir / "design.sdc"
    shutil.copy(_BAD_SDC, sdc_copy)

    monkeypatch.chdir(cwd)
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "-s",
            str(sdc_copy),
            "--emit-domain-map",
            "maps/dm.json",
            "--no-findings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (sdc_dir / "maps" / "dm.json").exists()
    assert not (cwd / "maps" / "dm.json").exists()


def test_relative_emit_defaults_to_cwd_when_unanchored(tmp_path, monkeypatch) -> None:
    """No ``--project-root`` and no ``--sdc`` → legacy cwd resolution
    (back-compat)."""
    _skip_if_missing(_BAD_JSON)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(_BAD_JSON),
            "--emit-domain-map",
            "dm.json",
            "--no-findings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "dm.json").exists()


def test_absolute_emit_unaffected_by_project_root(tmp_path, monkeypatch) -> None:
    """An absolute ``--emit-*`` path is written as-is even when a
    ``--project-root`` is supplied."""
    _skip_if_missing(_BAD_JSON, _BAD_SDC)
    root = tmp_path / "root"
    root.mkdir()
    abs_target = tmp_path / "absolute" / "dm.json"

    result = _emit(
        ["--project-root", str(root), "--emit-domain-map", str(abs_target)],
        tmp_path,
        monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert abs_target.exists()
    assert not (root / "absolute").exists()


def test_lint_yosys_plugin_anchored_to_project_root(tmp_path, monkeypatch) -> None:
    """A relative ``--yosys-plugin`` is resolved against ``--project-root``
    before the frontend sees it. We patch ``elaborate`` to capture the
    string and short-circuit (no real yosys/plugin needed)."""
    sv = tmp_path / "m.sv"
    sv.write_text("module m(input logic c, output logic q); endmodule\n")
    root = tmp_path / "root"
    cwd = tmp_path / "deep"
    root.mkdir()
    cwd.mkdir()

    captured: dict[str, object] = {}

    def _capture(*args, **kwargs):
        captured["plugin"] = kwargs.get("yosys_plugin")
        raise YosysError("captured")

    monkeypatch.setattr(cli_mod, "elaborate", _capture)
    monkeypatch.chdir(cwd)
    result = runner.invoke(
        app,
        [
            "lint",
            str(sv),
            "--top",
            "m",
            "--project-root",
            str(root),
            "--yosys-plugin",
            "build/slang.so",
        ],
    )
    assert result.exit_code == 2  # YosysError → exit 2
    assert captured["plugin"] == str(root / "build" / "slang.so")
