from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "CDC" in result.stdout


def test_version_runs() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "rtl-buddy-cdc" in result.stdout


def test_version_reports_pyslang_line() -> None:
    """The version command must always emit a pyslang status line — a
    bare ``pyslang: <version>`` when the optional extra is installed,
    or ``pyslang: not installed`` otherwise. Bug reports involving
    ``--frontend slang`` benefit from this diagnostic; the line being
    *unconditional* is the contract (#25)."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "pyslang:" in result.stdout
