"""Write the API's OpenAPI schema to a file without touching Postgres, Redis or a chain.

Usage: uv run python -m scripts.export_openapi ../../packages/shared/openapi.json
The app object is built (lifespan NOT started), so nothing connects anywhere. Output is stable
(sorted keys, fixed indent, trailing newline) so CI can fail on drift with `git diff`.
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("API_ENV", "development")  # schema export needs no production secrets

from app.main import create_app


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: export_openapi <output.json>", file=sys.stderr)
        return 2
    schema = create_app().openapi()
    Path(argv[1]).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
