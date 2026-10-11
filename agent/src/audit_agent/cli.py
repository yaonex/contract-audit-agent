"""CLI entrypoint: audit / eval / index-kb."""
from pathlib import Path

import typer

app = typer.Typer(help="Smart contract security audit agent.", no_args_is_help=True)


@app.command()
def audit(path: Path = typer.Argument(..., help="Solidity file or project dir")) -> None:
    """Run an audit on a contract."""
    typer.echo(f"[todo] audit {path}")

@app.command()
def eval(dataset: str = typer.Option("smartbugs", help="Dataset name")) -> None:
    """Run evaluation on a dataset."""
    typer.echo(f"[todo] eval {dataset}")

@app.command(name="index-kb")
def index_kb() -> None:
    """Build the RAG knowledge base."""
    typer.echo("[todo] index-kb")


if __name__ == "__main__":
    app()
