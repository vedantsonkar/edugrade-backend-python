"""Start the API after loading any configured AWS runtime secrets."""

import os
import subprocess

from app.core.secrets.aws import load_runtime_secrets


def main() -> None:
    """Load credentials, apply migrations, and replace the process with Uvicorn."""
    os.environ.update(load_runtime_secrets(os.environ))
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    os.execvp(
        "uvicorn",
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port",
         os.environ.get("PORT", "8080"), "--workers", os.environ.get("WORKERS", "1")],
    )


if __name__ == "__main__":
    main()
