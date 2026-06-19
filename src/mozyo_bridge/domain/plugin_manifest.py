"""Static plugin manifest schema / validator as review metadata (Redmine #12250).

This is a pure, **static** schema boundary for a future plugin distribution
*review* manifest, designed against the built-in adapter boundary in
``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine #12001). It lets
the codebase *describe and review* a candidate plugin as declarative metadata —
which adapter categories it claims, what mechanics it says it performs, what
permissions it requests, what safety invariants it promises — **without** any of
the machinery a real plugin system would need.

It is deliberately **not** a plugin loader, and it is the same fail-closed shape
as the sibling config records (``domain/repo_local_config.py``,
``domain/provider_registry.py``, ``domain/module_registry.py``):

- **No execution, ever.** The manifest carries declarative metadata only. There
  is no dynamic import, entry point, callable, shell command, install/run hook,
  or runtime loading — and not merely by omission: any key shaped like one
  (``import`` / ``entry_point`` / ``callable`` / ``exec`` / ``shell`` /
  ``command`` / ``install`` / ``run`` / ``hook`` / ``script`` / ``spawn`` …) is
  rejected at validation through :class:`PluginManifestError`. This validator
  reads an already-parsed mapping; it does no file IO and runs no manifest code.
- **Fail closed on boundary leaks.** A value that looks like a private /
  absolute / home filesystem path, or a secret-shaped string (token / secret /
  password / api key / credential …), is rejected wherever it appears. A
  ``declared_permission`` that names a core-owned authority — workflow / owner /
  close / review / routing / send — or a destructive / install / shell capability
  is rejected. The forbidden-authority set is sourced from
  :data:`~mozyo_bridge.domain.provider_registry.FORBIDDEN_PROVIDER_AUTHORITIES`
  so a manifest permission and a registered provider are screened against the
  same core-owned list and cannot drift.
- **No second source of truth for packaging metadata.** A plugin's packaging
  identity (``name`` / ``version`` / ``description`` / ``author`` / ``license``
  / ``homepage`` / ``repository`` / ``keywords`` …) already has a source of
  truth in ``.claude-plugin/marketplace.json`` and
  ``plugins/*/.claude-plugin/plugin.json`` (covered by
  ``tests/test_plugin_marketplace.py``). This review manifest stores **none** of
  them: such a key is rejected with a "duplicate packaging metadata" message, so
  there is no duplicated field and no sync obligation. ``plugin_id`` is a free
  correlation handle for review, explicitly *not* bound to the packaging
  ``name``, so it carries no drift risk either.
- **No public ABI / compatibility promise.** The closed key set, the category
  vocabulary, and the record shapes are internal and may change with no
  deprecation window. The categories a manifest may claim are the same
  core-owned :class:`~mozyo_bridge.domain.provider_registry.ProviderCategory`
  vocabulary, so a manifest can never invent an adapter category.

The module is pure (a frozen dataclass + small validation helpers) and imports
only the sibling provider-registry vocabulary, so the dependency only ever points
within the domain layer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from mozyo_bridge.domain.provider_registry import (
    FORBIDDEN_PROVIDER_AUTHORITIES,
    ProviderCategory,
)

#: The supported manifest record version. ``manifest_version`` is optional in a
#: record and defaults to this; any other value is rejected so a future,
#: not-yet-understood schema never reads as version 1.
PLUGIN_MANIFEST_VERSION: int = 1

#: The closed set of recognized top-level keys. Anything else fails closed. The
#: set is deliberately small and review-oriented; packaging identity fields are
#: *not* here (see :data:`PACKAGING_METADATA_FIELDS`).
PLUGIN_MANIFEST_KEYS: frozenset[str] = frozenset(
    {
        "manifest_version",
        "plugin_id",
        "summary",
        "categories",
        "capabilities",
        "declared_permissions",
        "safety_constraints",
        "experimental",
    }
)

#: Packaging-identity keys that already have a source of truth in the Claude
#: plugin manifests (``.claude-plugin/marketplace.json`` /
#: ``plugins/*/.claude-plugin/plugin.json``). This review manifest stores none of
#: them; one appearing as a key is rejected with a "second source of truth"
#: message rather than the generic unknown-key error, so the no-duplication
#: decision reads as deliberate in an audit.
PACKAGING_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "owner",
        "license",
        "homepage",
        "repository",
        "keywords",
        "source",
        "category",
    }
)

#: Substrings in a manifest *key* that signal an attempt to make the manifest do
#: something — load / execute code, name a module / callable / entry point, run an
#: install / build / shell command or a lifecycle hook, grant or alter authority /
#: approval / routing / send, address a target / pane, or carry a credential. Such
#: a key is rejected with a boundary-specific message. Keys are identifiers (not
#: prose), so this list is aggressive on purpose; none of :data:`PLUGIN_MANIFEST_KEYS`
#: contains any of these tokens.
_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    # dynamic import / entry point / callable / code loading
    "import",
    "module",
    "entry",
    "callable",
    "exec",
    "eval",
    "script",
    "load",
    "dynamic",
    "sys_path",
    # shell / install / run / lifecycle behavior
    "shell",
    "command",
    "cmd",
    "subprocess",
    "spawn",
    "install",
    "uninstall",
    "setup",
    "build",
    "compile",
    "run",
    "launch",
    "start",
    "activate",
    "bootstrap",
    "hook",
    # authority / approval / routing / send (core-owned)
    "authority",
    "authorities",
    "approval",
    "approve",
    "grant",
    "owner",
    "review",
    "close",
    "routing",
    "route",
    "send",
    "target",
    "pane",
    "role",
    # credential / secret
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "cookie",
    "billing",
)

#: Substrings that mark a string *value* as secret-shaped. Precise (not the
#: aggressive key list) so a review ``summary`` is not rejected for innocent
#: prose, while a value that names a credential is always refused. Vendor-prefix
#: / high-entropy detection is intentionally omitted to keep this module free of
#: secret-shaped literals (a declarative review manifest has no business carrying
#: a credential value of any shape; naming one is already a category error).
_SECRET_VALUE_TOKENS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_key",
    "access_token",
    "auth_token",
    "client_secret",
    "private_key",
    "credential",
    "cookie",
    "token",
)

#: Substrings that mark a ``declared_permission`` as authority-shaped or as
#: destructive / install / shell behavior. Combined with the exact core-owned
#: authority names from :data:`FORBIDDEN_PROVIDER_AUTHORITIES` (so the two cannot
#: drift), a permission matching any of these is rejected.
_FORBIDDEN_PERMISSION_PARTS: tuple[str, ...] = (
    "authority",
    "approval",
    "approve",
    "grant",
    "owner",
    "close",
    "review",
    "routing",
    "route",
    "send",
    "exec",
    "shell",
    "install",
    "uninstall",
    "spawn",
    "subprocess",
    "sudo",
    "admin",
    "destroy",
    "delete",
)

#: A Windows drive-letter path prefix (``C:\`` / ``C:/``), used by the
#: private-path check alongside POSIX-absolute and home-relative prefixes.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


class PluginManifestError(ValueError):
    """A plugin manifest record violates the static, declarative-only schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    domain errors (:class:`~mozyo_bridge.domain.provider_registry.ProviderRegistryError`,
    :class:`~mozyo_bridge.domain.repo_local_config.RepoLocalConfigError`).
    """


def _looks_like_private_path(value: str) -> bool:
    """True if ``value`` looks like an absolute / home / drive filesystem path.

    A static review manifest declares no filesystem paths, so any such value is a
    private-path leak. Recognizes POSIX-absolute (``/...``), home-relative
    (``~...``), Windows drive (``C:\\...``), and any backslash-bearing string.
    """
    if value.startswith("/") or value.startswith("~"):
        return True
    if "\\" in value:
        return True
    return bool(_WINDOWS_DRIVE.match(value))


def _looks_like_secret(value: str) -> bool:
    """True if ``value`` names a credential / secret (see :data:`_SECRET_VALUE_TOKENS`)."""
    lowered = value.lower()
    return any(token in lowered for token in _SECRET_VALUE_TOKENS)


def _reject_string_value(value: str, *, where: str) -> None:
    """Fail closed on a string value that leaks a private path or a secret."""
    if _looks_like_private_path(value):
        raise PluginManifestError(
            f"{where} value {value!r} looks like a private / absolute filesystem "
            f"path; a static review manifest declares no paths (fail-closed)."
        )
    if _looks_like_secret(value):
        raise PluginManifestError(
            f"{where} value names a credential / secret; a declarative review "
            f"manifest may never carry a token / secret / password / api key / "
            f"credential value (fail-closed)."
        )


def _reject_boundary_key(key: str, *, where: str) -> None:
    """Fail closed on a key that names code loading / run / authority / credential."""
    lowered = key.lower()
    for part in _FORBIDDEN_KEY_PARTS:
        if part in lowered:
            raise PluginManifestError(
                f"{where} key {key!r} may not carry a boundary token: a static "
                f"review manifest is declarative-only and may never load code, "
                f"name a module / callable / entry point, run a shell / install / "
                f"build command or lifecycle hook, grant authority, address a "
                f"target / pane, or carry a credential (matched forbidden token "
                f"{part!r})."
            )


def _scan_for_boundary_leaks(value: object, *, where: str) -> None:
    """Recursively reject boundary-crossing keys and path / secret string values.

    Runs *before* the typed parse so a forbidden key or a leaked path / secret is
    refused no matter how deeply it is nested (e.g. a credential hidden inside a
    ``capabilities`` entry, or an ``entry_point`` key inside a nested table). A
    mapping's keys are screened by :func:`_reject_boundary_key`; every string —
    key or value — is screened by :func:`_reject_string_value`; ``int`` / ``bool``
    / ``None`` carry no boundary token and are passed over.
    """
    if isinstance(value, Mapping):
        for key, sub in value.items():
            if isinstance(key, str):
                _reject_boundary_key(key, where=where)
                _reject_string_value(key, where=f"{where} key")
            _scan_for_boundary_leaks(sub, where=where)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _scan_for_boundary_leaks(item, where=where)
    elif isinstance(value, str):
        _reject_string_value(value, where=where)


def _label_set(value: object, *, field_name: str) -> frozenset[str]:
    """Normalize a label collection into a validated ``frozenset`` of strings.

    A bare ``str``/``bytes`` is **rejected** rather than normalized: both are
    iterable, so ``frozenset("owner")`` would silently explode into single
    characters and could slip past the authority / boundary checks. Each entry
    must be a non-empty ``str``; any other input raises
    :class:`PluginManifestError`. ``list`` / ``tuple`` / ``set`` / ``frozenset``
    of strings normalize as expected. (Same guard as the provider registry's
    ``_frozen_label_set``.)
    """
    if isinstance(value, (str, bytes)):
        raise PluginManifestError(
            f"{field_name} must be a collection of strings, not a bare "
            f"{type(value).__name__}; a bare string is iterated character-by-"
            f"character and would bypass the boundary checks."
        )
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise PluginManifestError(
            f"{field_name} must be an iterable of strings, got "
            f"{type(value).__name__}"
        ) from exc
    for item in items:
        if not isinstance(item, str) or not item:
            raise PluginManifestError(
                f"{field_name} entries must be non-empty strings; got {item!r}"
            )
    return frozenset(items)


def _normalize_categories(value: object) -> frozenset[ProviderCategory]:
    """Normalize declared category names into known :class:`ProviderCategory` set.

    Accepts an iterable of category names (strings) or :class:`ProviderCategory`
    members; a bare ``str``/``bytes`` is rejected (char explosion guard). Each
    name must be a known core-owned category value — a manifest can never invent
    an adapter category — and an unknown name fails closed.
    """
    if isinstance(value, (str, bytes)):
        raise PluginManifestError(
            "categories must be a collection of category names, not a bare "
            f"{type(value).__name__}"
        )
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise PluginManifestError(
            f"categories must be an iterable of category names, got "
            f"{type(value).__name__}"
        ) from exc
    known = {c.value for c in ProviderCategory}
    resolved: set[ProviderCategory] = set()
    for item in items:
        if isinstance(item, ProviderCategory):
            resolved.add(item)
            continue
        if not isinstance(item, str) or not item:
            raise PluginManifestError(
                f"category entries must be non-empty category names; got {item!r}"
            )
        if item not in known:
            raise PluginManifestError(
                f"unknown plugin category {item!r}; known core-owned categories: "
                f"{sorted(known)}. A manifest may not invent an adapter category."
            )
        resolved.add(ProviderCategory(item))
    return frozenset(resolved)


def _reject_authority_permission(permission: str) -> None:
    """Fail closed on a ``declared_permission`` that names a core-owned authority.

    Rejects the exact :data:`FORBIDDEN_PROVIDER_AUTHORITIES` names (so a manifest
    permission and a registered provider are screened against the same list) and
    any permission whose name contains an authority / approval / routing / send or
    destructive / install / shell token (:data:`_FORBIDDEN_PERMISSION_PARTS`).
    """
    lowered = permission.lower()
    if permission in FORBIDDEN_PROVIDER_AUTHORITIES or any(
        part in lowered for part in _FORBIDDEN_PERMISSION_PARTS
    ):
        raise PluginManifestError(
            f"declared permission {permission!r} is authority-shaped or names a "
            f"destructive / install / shell behavior; workflow / owner / close / "
            f"review / routing / send authority stays core-owned and may never be "
            f"a plugin permission (fail-closed)."
        )


def _checked_manifest_version(record: "Mapping[object, object]") -> int:
    """Return the supported version, failing closed on anything else.

    ``manifest_version`` is optional and defaults to
    :data:`PLUGIN_MANIFEST_VERSION`. ``bool`` is rejected even though it is an
    ``int`` subclass so ``manifest_version: true`` does not read as version 1.
    """
    version = record.get("manifest_version", PLUGIN_MANIFEST_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise PluginManifestError(
            f"manifest 'manifest_version' must be an integer, got {version!r}"
        )
    if version != PLUGIN_MANIFEST_VERSION:
        raise PluginManifestError(
            f"unsupported plugin manifest version {version!r}; this build "
            f"understands version {PLUGIN_MANIFEST_VERSION}"
        )
    return version


@dataclass(frozen=True)
class PluginManifest:
    """A pure, static description of a candidate plugin — review metadata, not code.

    Fields (all declarative; the record holds no behavior and no handle to code):

    - ``plugin_id``: a stable correlation handle for review. Required, non-empty.
      It is *not* the packaging ``name`` and is bound to nothing in the packaging
      manifests, so it introduces no duplicated field and no sync obligation.
    - ``summary``: one-line, public-safe review note (default empty). Distinct
      from the packaging ``description``, which this manifest never stores.
    - ``categories``: the core-owned
      :class:`~mozyo_bridge.domain.provider_registry.ProviderCategory` members the
      plugin claims to serve. A manifest may not invent a category.
    - ``capabilities``: descriptive labels for the mechanics the plugin says it
      performs (e.g. ``"normalize_issue"``). Purely descriptive.
    - ``declared_permissions``: labels for what the plugin requests. None may be
      authority-shaped or name destructive / install / shell behavior.
    - ``safety_constraints``: invariants the plugin promises to uphold. Recorded
      for review; the schema does not execute them.
    - ``experimental``: ``True`` marks not-yet-stable review metadata.

    Construct directly with already-typed values, or validate an already-parsed
    mapping with :meth:`from_record` / :func:`validate_plugin_manifest`.
    """

    plugin_id: str = ""
    summary: str = ""
    categories: frozenset[ProviderCategory] = frozenset()
    capabilities: frozenset[str] = frozenset()
    declared_permissions: frozenset[str] = frozenset()
    safety_constraints: frozenset[str] = frozenset()
    experimental: bool = False
    manifest_version: int = PLUGIN_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.manifest_version, bool) or not isinstance(
            self.manifest_version, int
        ):
            raise PluginManifestError(
                f"manifest_version must be an integer, got {self.manifest_version!r}"
            )
        if self.manifest_version != PLUGIN_MANIFEST_VERSION:
            raise PluginManifestError(
                f"unsupported plugin manifest version {self.manifest_version!r}; "
                f"this build understands version {PLUGIN_MANIFEST_VERSION}"
            )
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise PluginManifestError("plugin_id must be a non-empty string")
        # plugin_id is a free-form correlation slug carrying no behavior, so it is
        # only screened for a private-path / secret leak — not the aggressive
        # key-token list (which would falsely reject a slug like
        # "sample-review-plugin" on the substring "review").
        _reject_string_value(self.plugin_id, where="plugin_id")
        if not isinstance(self.summary, str):
            raise PluginManifestError(
                f"summary must be a string, got {type(self.summary).__name__}"
            )
        _reject_string_value(self.summary, where="summary")
        if not isinstance(self.experimental, bool):
            raise PluginManifestError(
                f"experimental must be a bool, got {type(self.experimental).__name__}"
            )

        categories = _normalize_categories(self.categories)
        capabilities = _label_set(self.capabilities, field_name="capabilities")
        for cap in capabilities:
            _reject_string_value(cap, where="capability")
        permissions = _label_set(
            self.declared_permissions, field_name="declared_permissions"
        )
        for perm in permissions:
            _reject_string_value(perm, where="declared permission")
            _reject_authority_permission(perm)
        safety = _label_set(self.safety_constraints, field_name="safety_constraints")
        for constraint in safety:
            _reject_string_value(constraint, where="safety constraint")

        object.__setattr__(self, "categories", categories)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "declared_permissions", permissions)
        object.__setattr__(self, "safety_constraints", safety)

    @classmethod
    def from_record(cls, record: object) -> "PluginManifest":
        """Validate an already-parsed manifest mapping, returning the typed record.

        ``record`` is the in-memory mapping a ``json.load`` / ``yaml.safe_load`` of
        a manifest file would yield; this layer does **no** file IO and runs no
        manifest code. Fail-closed, in order:

        - a non-mapping record (incl. ``None``) is rejected — a manifest is a
          table, and there is no behavior-preserving "empty manifest";
        - each top-level key is classified, in precedence order: a recognized
          manifest key is kept; a packaging-identity key
          (:data:`PACKAGING_METADATA_FIELDS`) is rejected as a forbidden second
          source of truth; a key naming code loading / run / authority / credential
          is rejected as a boundary token; any other key is an unknown key (closed
          schema / typo guard). Packaging precedes the boundary check so a field
          like ``description`` (which contains the substring ``script``) reports
          the precise duplication reason rather than a misleading boundary match;
        - each top-level *value* is then scanned recursively, rejecting any nested
          key naming a boundary token and any string (key or value, at any depth)
          that leaks a private path or a secret;
        - ``manifest_version``, if present, must be the supported integer version;
        - ``plugin_id`` is required; the remaining declarative fields are parsed
          and validated by :meth:`__post_init__` (category vocabulary, label
          shapes, authority-shaped permissions, types).
        """
        if not isinstance(record, Mapping):
            raise PluginManifestError(
                "plugin manifest record must be a mapping (a JSON/YAML table), got "
                f"{type(record).__name__}"
            )

        for key in record:
            if not isinstance(key, str) or not key:
                raise PluginManifestError(
                    f"plugin manifest record keys must be non-empty strings; got "
                    f"{key!r}"
                )
            if key in PLUGIN_MANIFEST_KEYS:
                continue
            if key in PACKAGING_METADATA_FIELDS:
                raise PluginManifestError(
                    f"plugin manifest may not duplicate packaging metadata field "
                    f"{key!r}: it already has a source of truth in the Claude plugin "
                    f"manifests (.claude-plugin/marketplace.json / "
                    f"plugins/*/.claude-plugin/plugin.json). This review manifest "
                    f"stores no packaging fields (no second source of truth)."
                )
            _reject_boundary_key(key, where="plugin manifest")
            raise PluginManifestError(
                f"plugin manifest record has unknown key {key!r}; allowed keys: "
                f"{sorted(PLUGIN_MANIFEST_KEYS)}"
            )

        # Top-level keys are now classified; scan each value recursively for a
        # nested forbidden key (e.g. an ``entry_point`` buried in a capability)
        # and for a private-path / secret leak at any depth.
        for value in record.values():
            _scan_for_boundary_leaks(value, where="plugin manifest")

        _checked_manifest_version(record)
        if "plugin_id" not in record:
            raise PluginManifestError(
                "plugin manifest record must name a 'plugin_id' (the review "
                "correlation handle)."
            )

        return cls(
            plugin_id=record["plugin_id"],
            summary=record.get("summary", ""),
            categories=record.get("categories", ()),
            capabilities=record.get("capabilities", ()),
            declared_permissions=record.get("declared_permissions", ()),
            safety_constraints=record.get("safety_constraints", ()),
            experimental=record.get("experimental", False),
            manifest_version=record.get("manifest_version", PLUGIN_MANIFEST_VERSION),
        )

    @property
    def category_names(self) -> tuple[str, ...]:
        """The claimed category values, sorted, as plain strings (a stable view)."""
        return tuple(sorted(c.value for c in self.categories))


def validate_plugin_manifest(record: object) -> PluginManifest:
    """Validate an already-parsed manifest mapping; the validator entry point.

    Thin alias for :meth:`PluginManifest.from_record`. Returns the typed
    :class:`PluginManifest` on success or raises :class:`PluginManifestError`.
    Does no file IO and runs no manifest code.
    """
    return PluginManifest.from_record(record)


__all__ = (
    "PLUGIN_MANIFEST_VERSION",
    "PLUGIN_MANIFEST_KEYS",
    "PACKAGING_METADATA_FIELDS",
    "PluginManifestError",
    "PluginManifest",
    "validate_plugin_manifest",
)
