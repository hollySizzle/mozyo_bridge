"""Local-only docs catalog overlay (Redmine #11819).

The public ``catalog.yaml`` is an OSS artifact: it must not carry
private consumer paths, operator runbooks, or any secret-shaped value
(see ``vibes/docs/rules/public-private-boundary.md``). But a checkout
that *does* have local-only docs / rules still wants the agent docs
resolver to surface them automatically instead of relying on an agent
remembering to read them by hand.

This module adds an *optional* overlay: a git-ignored
``catalog.local.yaml`` sitting next to the public catalog. When the
overlay is present, the local-facing commands (``docs resolve`` /
``docs audit-impact``) merge it on top of the public catalog so the
extra documents / file_conventions resolve like any other entry. When
the overlay is absent — fresh clone, CI, PyPI consumer — nothing
changes and the public catalog is used verbatim.

Two boundaries keep the overlay from leaking private facts into public
artifacts:

* **Separation** — the public commands (``docs validate`` /
  ``docs generate-file-conventions``) never read the overlay, so the
  tracked, generated ``file_conventions.generated.yaml`` can never
  contain overlay-only data.
* **Secret-shaped guard** — the overlay is meant to hold private
  *paths*, but never credentials. Loading the overlay scans it for
  secret-shaped values (credential-named keys, well-known token
  prefixes, PEM private-key blocks) and fails closed if any are found,
  reporting only the *location* — never the value itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .catalog import CatalogContext, DEFAULT_OVERLAY_FILENAME, load_catalog


# Only these top-level keys make sense in an overlay: it supplements the
# public catalog's documents / file_conventions. Anything else (e.g. a
# stray ``coverage_roots``) is almost certainly an operator mistake and
# is rejected rather than silently ignored.
OVERLAY_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "documents", "file_conventions"}
)

# Mapping keys whose presence with a non-empty scalar value indicates a
# credential leaked into the overlay. Catalog metadata never needs these.
CREDENTIAL_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "api_token",
        "access_key",
        "secret_key",
        "private_key",
        "cookie",
        "authorization",
        "bearer",
        "session_token",
        "credential",
        "credentials",
    }
)

# Well-known secret value shapes. Conservative on purpose: catalog
# overlays hold ids / paths / prose, so these patterns almost never
# false-positive on legitimate content.
SECRET_VALUE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
)


class OverlayError(ValueError):
    """Overlay is malformed, collides with the public catalog, or carries
    a secret-shaped value.

    Inherits ``ValueError`` so existing ``except ValueError`` handlers
    around catalog loading keep working.
    """


@dataclass(frozen=True)
class OverlayInfo:
    """Result of resolving the overlay for one invocation.

    ``applied`` is True only when an overlay was present *and* contributed
    at least one document or file_convention. ``path`` is the absolute
    overlay path when it exists on disk (regardless of whether it
    contributed anything), else ``None``.
    """

    applied: bool
    path: Path | None = None
    document_count: int = 0
    file_convention_count: int = 0

    def display_path(self, repo_root: Path | None = None) -> str | None:
        """Repo-relative overlay path for human-facing notices, when known."""
        if self.path is None:
            return None
        if repo_root is not None:
            try:
                return self.path.relative_to(repo_root).as_posix()
            except ValueError:
                pass
        return self.path.as_posix()


def _scan(node: Any, location: str, findings: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{location}.{key}"
            if (
                isinstance(key, str)
                and key.strip().lower() in CREDENTIAL_KEY_NAMES
                and _is_non_empty_scalar(value)
            ):
                findings.append(f"{child}: credential-named key holds a value")
            _scan(value, child, findings)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan(item, f"{location}[{index}]", findings)
    elif isinstance(node, str):
        for kind, pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(node):
                findings.append(f"{location}: matches {kind} pattern")
                break


def _is_non_empty_scalar(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    return isinstance(value, (int, float))


def scan_for_secret_shaped_values(data: Any, *, location: str = "overlay") -> list[str]:
    """Return secret-shaped findings as ``location: reason`` strings.

    The returned strings deliberately never include the offending value
    so they are safe to print to logs or echo into a Redmine journal.
    """
    findings: list[str] = []
    _scan(data, location, findings)
    return findings


def read_overlay_document(overlay_path: Path) -> dict[str, Any] | None:
    """Read + structurally gate the overlay YAML (no secret scan).

    Returns ``None`` when the overlay file is absent, ``{}`` when it is
    present but empty, otherwise the parsed mapping. Raises
    :class:`OverlayError` on invalid YAML, a non-mapping root, an
    unsupported top-level key, or a bad ``schema_version``.
    """
    if not overlay_path.exists():
        return None
    try:
        data = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OverlayError(f"overlay is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise OverlayError("overlay root must be a mapping")
    extra = set(data) - OVERLAY_ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise OverlayError(
            "overlay has unsupported top-level keys: "
            f"{sorted(extra)} (allowed: {sorted(OVERLAY_ALLOWED_TOP_LEVEL_KEYS)})"
        )
    schema_version = data.get("schema_version")
    if schema_version is not None and schema_version != 1:
        raise OverlayError("overlay schema_version must be 1 when present")
    return data


def load_overlay(overlay_path: Path) -> dict[str, Any] | None:
    """Read the overlay and fail closed on secret-shaped values.

    Used by the local-facing resolve / audit-impact path: an overlay
    that carries a credential stops the workflow rather than silently
    feeding it into the resolver.
    """
    data = read_overlay_document(overlay_path)
    if not data:
        return data
    findings = scan_for_secret_shaped_values(data)
    if findings:
        raise OverlayError(
            "overlay contains secret-shaped value(s); catalog overlays hold "
            "paths, never credentials. Remove:\n"
            + "\n".join(f"- {finding}" for finding in findings)
        )
    return data


def merge_catalog_with_overlay(
    base: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    """Return a new catalog with overlay documents / file_conventions added.

    The merge is strictly additive: overlay entries must introduce new
    ids, never shadow a public id. A collision raises
    :class:`OverlayError` so a local file can never silently override the
    public source of truth.
    """
    merged = dict(base)

    base_documents = list(base.get("documents", []))
    overlay_documents = list(overlay.get("documents", []))
    base_document_ids = {
        doc.get("id") for doc in base_documents if isinstance(doc, dict)
    }
    for doc in overlay_documents:
        if isinstance(doc, dict) and doc.get("id") in base_document_ids:
            raise OverlayError(
                f"overlay document id `{doc.get('id')}` collides with a public "
                "catalog id; overlays must add new ids, not shadow public docs"
            )
    merged["documents"] = base_documents + overlay_documents

    base_conventions = list(base.get("file_conventions", []))
    overlay_conventions = list(overlay.get("file_conventions", []))
    base_convention_ids = {
        item.get("id") for item in base_conventions if isinstance(item, dict)
    }
    for item in overlay_conventions:
        if isinstance(item, dict) and item.get("id") in base_convention_ids:
            raise OverlayError(
                f"overlay file_convention id `{item.get('id')}` collides with a "
                "public catalog id; overlays must add new ids"
            )
    merged["file_conventions"] = base_conventions + overlay_conventions

    return merged


def _overlay_contributes(overlay: dict[str, Any] | None) -> bool:
    if not overlay:
        return False
    return bool(overlay.get("documents") or overlay.get("file_conventions"))


def load_effective_catalog(
    context: CatalogContext,
    *,
    include_local: bool = True,
) -> tuple[dict[str, Any], OverlayInfo]:
    """Load the public catalog, optionally merged with the local overlay.

    Returns ``(catalog, overlay_info)``. With ``include_local=False`` —
    or when no overlay file exists — the public catalog is returned
    unchanged, exactly matching the fresh-clone / CI behaviour.
    """
    base = load_catalog(context.catalog_path)
    if not include_local:
        return base, OverlayInfo(applied=False)

    overlay_path = context.overlay_path
    overlay = load_overlay(overlay_path)
    overlay_exists = overlay_path.exists()
    if not _overlay_contributes(overlay):
        return base, OverlayInfo(
            applied=False,
            path=overlay_path if overlay_exists else None,
        )

    assert overlay is not None  # _overlay_contributes guarantees this
    merged = merge_catalog_with_overlay(base, overlay)
    return merged, OverlayInfo(
        applied=True,
        path=overlay_path,
        document_count=len(overlay.get("documents", [])),
        file_convention_count=len(overlay.get("file_conventions", [])),
    )


__all__ = [
    "CREDENTIAL_KEY_NAMES",
    "DEFAULT_OVERLAY_FILENAME",
    "OVERLAY_ALLOWED_TOP_LEVEL_KEYS",
    "OverlayError",
    "OverlayInfo",
    "SECRET_VALUE_PATTERNS",
    "load_effective_catalog",
    "load_overlay",
    "merge_catalog_with_overlay",
    "read_overlay_document",
    "scan_for_secret_shaped_values",
]
