"""Trigger a local UAT job using the runtime secret without a secret env file."""

import json
import subprocess
import sys
import urllib.parse
import urllib.request


def main() -> None:
    """Read the cron token and invoke the requested private endpoint."""
    job = sys.argv[1]
    if job not in {"grading", "archive"}:
        raise ValueError("Unsupported job")
    raw = subprocess.check_output(
        ["aws", "secretsmanager", "get-secret-value", "--region", "ap-southeast-2",
         "--secret-id", "edugrade/uat/backend", "--query", "SecretString", "--output", "text"],
        text=True,
    )
    token = json.loads(raw)["CRON_SECRET"]
    query = urllib.parse.urlencode({"key": token})
    with urllib.request.urlopen(f"http://127.0.0.1:18080/api/v1/cron/{job}?{query}", timeout=30) as response:
        print(f"{job}: HTTP {response.status}")


if __name__ == "__main__":
    main()
