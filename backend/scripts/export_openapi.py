"""Write the backend OpenAPI schema to a file (input to the web client's type generation).

    python scripts/export_openapi.py ../web/src/api/openapi.json

Builds the app with no verifier and no model: nothing is read from the environment and nothing
is called. The schema holds contract shapes only (no secrets, no user data).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import create_app  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: export_openapi.py OUTPUT.json")
        return 2
    out = Path(args[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
