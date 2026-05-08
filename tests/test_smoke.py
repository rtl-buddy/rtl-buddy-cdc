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
