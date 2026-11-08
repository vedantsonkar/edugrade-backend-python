"""Initialize the development object bucket and browser upload CORS rules."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.storage.s3 import SeaweedS3Storage


async def main() -> None:
    """Configure the selected bucket without modifying its stored objects."""
    settings = get_settings()
    await SeaweedS3Storage(settings).initialize_bucket()
    print("Object bucket ready; browser CORS uses the compose s3.allowedOrigins flag.")


if __name__ == "__main__":
    asyncio.run(main())
