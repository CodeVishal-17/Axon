"""Export the FastAPI OpenAPI schema to a JSON file.

First half of the type-generation pipeline (`make types`): this schema is
fed to openapi-typescript, which emits apps/web/lib/api/types.generated.ts.
The frontend imports ONLY those generated types for API payloads — that is
the contract that makes a split-language stack safe.

Usage (from apps/api/):
    python scripts/export_openapi.py [output_path]

Default output: ../web/openapi.json (gitignored intermediate; the generated
.ts file is what gets committed).
"""

import json
import sys
from pathlib import Path

# Make `import axon` work when invoked as a plain script from apps/api/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axon.main import create_app  # noqa: E402


def main() -> None:
    output = Path(
        sys.argv[1] if len(sys.argv) > 1 else "../web/openapi.json"
    ).resolve()

    app = create_app()
    schema = app.openapi()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")

    paths = len(schema.get("paths", {}))
    schemas = len(schema.get("components", {}).get("schemas", {}))
    print(f"wrote {output} ({paths} paths, {schemas} schemas)")


if __name__ == "__main__":
    main()
