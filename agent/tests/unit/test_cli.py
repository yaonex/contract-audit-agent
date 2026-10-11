from typer.testing import CliRunner

from audit_agent.cli import app

runner = CliRunner()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("audit", "eval", "index-kb"):
        assert cmd in result.output