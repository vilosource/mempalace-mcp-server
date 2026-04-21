"""mempalace-server CLI.

M1 ships `migrate` only. `status | logs | stop | restart | upgrade | backup | shell`
land in M3.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from server.migrate import migrate as run_migrate

app = typer.Typer(no_args_is_help=True)


@app.command()
def migrate(
    data_root: Path = typer.Argument(..., help="Palace data root to migrate."),
    embedding_model: str = typer.Option(
        "all-MiniLM-L6-v2",
        "--embedding-model",
        help="Embedding model to pin into palace config.json.",
    ),
    embedding_dim: int = typer.Option(
        384,
        "--embedding-dim",
        help="Embedding dimension to pin.",
    ),
    snapshot_taken: bool = typer.Option(
        False,
        "--snapshot-taken",
        help="Operator confirms a backup exists; migration refuses to run without this.",
    ),
):
    """Migrate an existing palace data root for use by the server."""
    result = run_migrate(
        data_root,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        snapshot_taken=snapshot_taken,
    )
    typer.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
