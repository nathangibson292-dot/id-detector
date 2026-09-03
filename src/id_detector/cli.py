"""Command-line entry point."""

import typer

from id_detector.doctor import run_doctor

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.callback()
def main() -> None:
    """Evidence-first DJ-set identification."""


@app.command()
def doctor() -> None:
    """Check the Stage 0 runtime and offline signature-generation path."""
    raise typer.Exit(run_doctor())


if __name__ == "__main__":
    app()
