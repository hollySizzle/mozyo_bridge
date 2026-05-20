"""Generate a file_conventions YAML from the catalog.

Distributed by the mozyo-bridge `redmine-rails-governed` preset. The
default output path is `.mozyo-bridge/docs/file_conventions.generated.yaml`,
which is a generic, project-neutral location. If the project needs to feed
a specific consumer (for example, a nagger configuration in some other
directory) pass `--output <path>` to point at the consumer's expected
location. The generated file is NOT the source of truth; the catalog is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from docs_catalog import CATALOG_PATH, REPO_ROOT, build_file_conventions_payload, load_catalog

HEADER = "# This file is generated from .mozyo-bridge/docs/catalog.yaml.\n# Do not edit manually.\n"
DEFAULT_OUTPUT = Path(".mozyo-bridge/docs/file_conventions.generated.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate file_conventions YAML from catalog")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG_PATH,
        help="Path to catalog YAML",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output YAML path (defaults to .mozyo-bridge/docs/file_conventions.generated.yaml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether the output file is up to date without writing",
    )
    args = parser.parse_args()

    output_path = args.output if args.output.is_absolute() else REPO_ROOT / args.output

    catalog = load_catalog(args.catalog)
    payload = build_file_conventions_payload(catalog)
    dumped = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )
    output_text = f"{HEADER}{dumped}"

    if args.check:
        if not output_path.exists():
            print(f"{output_path.as_posix()} does not exist", file=sys.stderr)
            return 1

        current_text = output_path.read_text(encoding="utf-8")
        if current_text != output_text:
            print(
                f"{output_path.as_posix()} is out of date. "
                f"Run `python3 .mozyo-bridge/tools/generate_file_conventions.py --output {args.output.as_posix()}`.",
                file=sys.stderr,
            )
            return 1

        print(f"{output_path.as_posix()} is up to date")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text, encoding="utf-8")
    print(output_path.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
