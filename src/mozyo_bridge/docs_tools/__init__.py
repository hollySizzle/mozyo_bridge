"""Docs catalog tooling distributed by the mozyo-bridge package.

Earlier governed-scaffold revisions vendor-copied these tools as Python
source under the target repo's ``.mozyo-bridge/tools/``. That mixed
project-local data and mozyo-bridge runtime in the same directory and
made upgrade / drift management harder. This package keeps the tooling
inside the installed CLI so target repos no longer carry the source —
operators run ``mozyo-bridge docs validate`` (etc.) and the same code
runs whether it was installed via pip, pipx, or from a wheel.

The public surface is deliberately small: each module exposes the
helpers other modules and the CLI rely on. ``catalog`` holds the pure
load / lookup primitives; ``validate`` / ``resolve`` / ``generate`` /
``impact`` hold the per-subcommand entry points.
"""

from .catalog import (
    CatalogContext,
    AUDIT_DOCUMENT_ORDER,
    DEFAULT_DOC_MESSAGE,
    DEFAULT_OVERLAY_FILENAME,
    MANAGED_TYPES,
    build_file_conventions_payload,
    load_catalog,
    matching_file_conventions,
    normalize_repo_relative_path,
    resolve_audit_documents,
)
from .generate import generate_file_conventions, run_generate_check
from .impact import (
    audit_doc_impact,
    audit_doc_impact_detailed,
    git_changed_paths,
)
from .overlay import (
    OverlayError,
    OverlayInfo,
    load_effective_catalog,
    load_overlay,
    merge_catalog_with_overlay,
    read_overlay_document,
    scan_for_secret_shaped_values,
)
from .resolve import (
    render_resolution_json,
    render_resolution_markdown,
    render_resolution_text,
    resolve_paths,
    resolve_paths_detailed,
)
from .validate import (
    DEFAULT_COVERAGE_ROOTS,
    resolve_coverage_roots,
    validate_catalog,
    validate_file_coverage,
    validate_overlay,
)

__all__ = [
    "CatalogContext",
    "AUDIT_DOCUMENT_ORDER",
    "DEFAULT_COVERAGE_ROOTS",
    "DEFAULT_DOC_MESSAGE",
    "DEFAULT_OVERLAY_FILENAME",
    "MANAGED_TYPES",
    "OverlayError",
    "OverlayInfo",
    "audit_doc_impact",
    "audit_doc_impact_detailed",
    "build_file_conventions_payload",
    "generate_file_conventions",
    "git_changed_paths",
    "load_catalog",
    "load_effective_catalog",
    "load_overlay",
    "matching_file_conventions",
    "merge_catalog_with_overlay",
    "normalize_repo_relative_path",
    "read_overlay_document",
    "render_resolution_json",
    "render_resolution_markdown",
    "render_resolution_text",
    "resolve_audit_documents",
    "resolve_coverage_roots",
    "resolve_paths",
    "resolve_paths_detailed",
    "run_generate_check",
    "scan_for_secret_shaped_values",
    "validate_catalog",
    "validate_file_coverage",
    "validate_overlay",
]
