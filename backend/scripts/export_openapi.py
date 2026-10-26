"""Write the backend OpenAPI schema to a file (input to the web client's type generation).

    python scripts/export_openapi.py ../web/src/api/openapi.json          # write
    python scripts/export_openapi.py --check ../web/src/api/openapi.json  # CI drift check

Builds the app with no verifier and no model: nothing is read from the environment and nothing
is called. The schema holds contract shapes only (no secrets, no user data).

``--check`` exits 0 when the file holds the same JSON as a fresh export (line endings and
whitespace are ignored, so a CRLF checkout does not count as drift), 1 when it differs or cannot be
read, and never writes. After an API change run ``npm run api:generate`` in ``web/``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import create_app  # noqa: E402


def fresh_schema() -> dict:
    return create_app().openapi()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check = args[:1] == ["--check"]
    paths = args[1:] if check else args
    if len(paths) != 1:
        print("usage: export_openapi.py [--check] OUTPUT.json")
        return 2
    out = Path(paths[0])
    schema = fresh_schema()
    if check:
        try:
            current = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"{out}: missing or not JSON; run npm run api:generate in web/")
            return 1
        if current != schema:
            print(f"{out} is out of date with the backend contract; run npm run api:generate in web/")
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
