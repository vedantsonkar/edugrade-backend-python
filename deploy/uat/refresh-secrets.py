"""Restart the UAT API when either Secrets Manager version changes."""

import fcntl
import json
import subprocess
import time
import urllib.request
from pathlib import Path

SECRET_IDS = (
    "edugrade/uat/backend",
    "arn:aws:secretsmanager:ap-southeast-2:592685884951:secret:"
    "rds!cluster-d169ec23-9628-4bea-a257-c9b8c7b6e058-vHiKql",
)
MARKER = Path("/opt/edugrade/secret-versions.json")


def current_version(secret_id: str) -> str:
    """Return the current version ID without reading or logging its value."""
    raw = subprocess.check_output(
        ["aws", "secretsmanager", "describe-secret", "--region", "ap-southeast-2",
         "--secret-id", secret_id, "--output", "json"],
        text=True,
    )
    versions = json.loads(raw)["VersionIdsToStages"]
    return next(version for version, stages in versions.items() if "AWSCURRENT" in stages)


def main() -> None:
    """Serialize with deployments and restart when credentials change."""
    with Path("/run/edugrade-deploy.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        versions = [current_version(secret_id) for secret_id in SECRET_IDS]
        previous = json.loads(MARKER.read_text()) if MARKER.exists() else None
        if previous == versions:
            return
        subprocess.run(["docker", "restart", "edugrade-api"], check=True, stdout=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18080/api/v1/ready", timeout=3):
                    MARKER.write_text(json.dumps(versions))
                    return
            except OSError:
                time.sleep(2)
        raise RuntimeError("API did not become ready after refreshing secrets")


if __name__ == "__main__":
    main()
