"""CLI entry point: audit / eval / index-kb."""
import typer

app = typer.Typer(help="Smart contract security audit agent")


@app.command()
def audit(path: str, out: str = "reports") -> None:
    """Audit a Solidity file and write Markdown + JSON reports."""
    raise NotImplementedError


@app.command()
def eval(split: str = "dev") -> None:
    """Run the benchmark evaluation."""
    raise NotImplementedError


@app.command("index-kb")
def index_kb(src: str) -> None:
    """Ingest the vulnerability knowledge base into Qdrant."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
