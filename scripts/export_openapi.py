#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the FastAPI OpenAPI schema to a JSON file.")
    parser.add_argument(
        "--output",
        default="docs/openapi.json",
        help="Output path for the generated OpenAPI JSON. Defaults to docs/openapi.json.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.environ["ENABLE_MCP_ADAPTER"] = "false"
    os.environ["ENABLE_OPENAI_APPS_UI"] = "false"

    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    schema = app.openapi()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
