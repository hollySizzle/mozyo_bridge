"""Generate the file_conventions output from the catalog.

The generated file is *not* source-of-truth — the catalog is. The
governed workflow runs the drift check (`--check`) before
Implementation Done / Review so the generated artifact never falls
out of sync silently.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .catalog import CatalogContext, build_file_conventions_payload, load_catalog


HEADER = "# This file is generated from .mozyo-bridge/docs/catalog.yaml.\n# Do not edit manually.\n"
DEFAULT_OUTPUT_RELATIVE_PATH = Path(".mozyo-bridge/docs/file_conventions.generated.yaml")


def _render_output(catalog: dict) -> str:
    payload = build_file_conventions_payload(catalog)
    dumped = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120)
    return f"{HEADER}{dumped}"


def _resolve_output_path(context: CatalogContext, output: Path | str | None) -> Path:
    if output is None:
        return context.repo_root / DEFAULT_OUTPUT_RELATIVE_PATH
    candidate = Path(output).expanduser()
    if candidate.is_absolute():
        return candidate
    return (context.repo_root / candidate).resolve()


def generate_file_conventions(
    context: CatalogContext,
    output: Path | str | None = None,
) -> Path:
    """Render the catalog payload to the requested output path. Returns the path."""
    catalog = load_catalog(context.catalog_path)
    output_path = _resolve_output_path(context, output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_output(catalog), encoding="utf-8")
    return output_path


def run_generate_check(
    context: CatalogContext,
    output: Path | str | None = None,
) -> tuple[bool, Path, str]:
    """Compare the recorded output against the catalog. Returns (ok, path, detail).

    ``ok`` is False when the on-disk output is missing or differs from
    the rendered payload. ``detail`` carries a human-readable message
    the CLI prints; callers in tests can also use it as the source of
    truth for the failure reason.
    """
    catalog = load_catalog(context.catalog_path)
    output_path = _resolve_output_path(context, output)
    expected = _render_output(catalog)
    if not output_path.exists():
        return False, output_path, f"{output_path.as_posix()} does not exist"
    current = output_path.read_text(encoding="utf-8")
    if current != expected:
        return (
            False,
            output_path,
            f"{output_path.as_posix()} is out of date; rerun `mozyo-bridge docs generate-file-conventions`.",
        )
    return True, output_path, f"{output_path.as_posix()} is up to date"
