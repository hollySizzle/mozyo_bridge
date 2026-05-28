"""Workspace defaults renderer (Redmine #10689).

A single workspace-local YAML (`<repo>/.mozyo-bridge/workspace-defaults.yaml`)
is the source of truth for repo-specific defaults that Codex / Claude /
local docs all reference. Today's slice covers the Redmine
default project resolution contract:

- the workspace declares a Redmine default project (identifier, name,
  url, parent label) plus a verification record (verified flag,
  verification date, verified_by);
- the renderer emits a generated Markdown snippet
  (`<repo>/.mozyo-bridge/redmine-defaults.md` by default) that agents
  reference from their session entrypoints;
- explicit `project_id` (in user instruction, ticket text, MCP request,
  session context) always wins over the default in the rendered
  resolution priority;
- an unverified default surfaces as `verified: NO — treat as a suggestion
  only` so agents do not silently treat it as fact.

Distributed mozyo_bridge code MUST NOT carry project-specific identifiers
(e.g. customer project codes). This module is the rendering pipeline; the
workspace-local YAML carries the values.

Secrets policy: credential / token / cookie shapes in the input YAML
are rejected at load time so the rendered Markdown cannot leak them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from mozyo_bridge.shared.errors import die


WORKSPACE_DEFAULTS_INPUT_RELATIVE = Path(".mozyo-bridge/workspace-defaults.yaml")
DEFAULT_REDMINE_OUTPUT_RELATIVE = Path(".mozyo-bridge/redmine-defaults.md")
SCHEMA_VERSION = 1

# Typed output kinds. Each kind has a dedicated renderer; adding a new
# kind is a code change (new render function + dispatch + tests + design
# doc update), NOT just adding another `target` line in the YAML. The
# schema rejects unknown kinds at load time so an operator cannot
# accidentally route Markdown content into a TOML / JSON config target.
#
# Per Codex review #50989: a generic `outputs[].target` is a footgun
# because every kind would otherwise inherit the same Markdown body.
# Typed dispatch makes the renderer extension contract explicit.
KIND_REDMINE_MARKDOWN = "redmine_markdown"
KNOWN_OUTPUT_KINDS = frozenset({KIND_REDMINE_MARKDOWN})

# Per-kind allowed target-path suffixes. Codex review #50995 caught the
# remaining footgun: `kind: redmine_markdown` alone does not stop an
# operator from pointing the target at `.codex/config.toml` or
# `.mcd.json`, which would still write Markdown body into a config
# path. The kind→suffix map blocks that at load time: each kind
# advertises the suffixes its renderer's output is valid for, and the
# loader rejects any target whose suffix is not in the kind's set.
#
# When adding a new kind, also declare its allowed suffixes here AND
# update the design doc's Supported Output Kinds table in the same
# commit.
KIND_ALLOWED_SUFFIXES: dict[str, frozenset[str]] = {
    KIND_REDMINE_MARKDOWN: frozenset({".md", ".markdown"}),
}

# Credential-shape patterns. Mirrors the tree-grep heuristics in
# `application.release._SECRET_VALUE_PATTERNS` so the workspace YAML
# gate is consistent with the release-flow Source Tree Hygiene gate.
# Anchored on standalone words to avoid false positives on prose like
# "session token expires" without a value assignment.
_SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "api-key",
        "apikey",
        "access_token",
        "access-token",
        "accesstoken",
        "refresh_token",
        "refresh-token",
        "refreshtoken",
        "client_secret",
        "client-secret",
        "clientsecret",
        "password",
        "secret",
        "cookie",
        "session_cookie",
        "auth_token",
        "auth-token",
        "bearer_token",
        "bearer-token",
    }
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"bearer[_-]?token|session[_-]?cookie|auth[_-]?token|password)\b\s*[:=]\s*[^<\s#][^\s#]*"
)
# Redmine / GitHub / PyPI / generic environment-variable token shapes
# that operators might paste in by accident. Same regex family as
# release.py's `_TREE_SECRET_VALUE_PATTERNS`.
_SECRET_ENV_RE = re.compile(
    r"(?i)\b(?:ASANA|GITHUB|PYPI|TWINE|REDMINE)[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY)\b\s*[:=]\s*[^<\s#][^\s#]*"
)


@dataclass(frozen=True)
class DefaultProject:
    identifier: str
    name: str
    url: str
    parent_label: str


@dataclass(frozen=True)
class Verification:
    verified: bool
    verification_date: str
    verified_by: str

    @property
    def is_complete(self) -> bool:
        """A verification record only counts when every field is set.

        A `verified: true` flag with an empty date or actor is the same
        as unverified for renderer purposes — the agent reading the
        output cannot tell when or by whom the check happened.
        """
        return (
            self.verified
            and bool(self.verification_date.strip())
            and bool(self.verified_by.strip())
        )


@dataclass(frozen=True)
class OutputSpec:
    kind: str
    target: Path


@dataclass(frozen=True)
class WorkspaceDefaults:
    schema_version: int
    default_project: DefaultProject
    verification: Verification
    outputs: tuple[OutputSpec, ...]
    source_path: Path


@dataclass(frozen=True)
class RenderResult:
    output_path: Path
    rendered: str
    on_disk: str | None

    @property
    def drift(self) -> bool:
        return self.on_disk != self.rendered

    @property
    def reason(self) -> str:
        return "missing" if self.on_disk is None else "out of date"


def _is_secret_key(name: str) -> bool:
    return name.strip().lower() in _SECRET_KEY_NAMES


def _value_looks_secret(value: str) -> bool:
    if _SECRET_VALUE_RE.search(value):
        return True
    if _SECRET_ENV_RE.search(value):
        return True
    return False


def _scan_for_secrets(raw: object, path: str, *, source: Path) -> None:
    """Reject credential-shape keys / values anywhere in the YAML tree."""
    if isinstance(raw, dict):
        for key, value in raw.items():
            key_str = str(key)
            child_path = f"{path}.{key_str}" if path else key_str
            if _is_secret_key(key_str):
                die(
                    f"workspace-defaults {source.as_posix()} carries "
                    f"credential-shape key {child_path!r}; remove the secret "
                    "and store it in an environment variable or system-managed "
                    "secret store instead."
                )
            _scan_for_secrets(value, child_path, source=source)
        return
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            _scan_for_secrets(item, f"{path}[{index}]", source=source)
        return
    if isinstance(raw, str) and _value_looks_secret(raw):
        die(
            f"workspace-defaults {source.as_posix()} value at {path!r} "
            "matches a credential-shape pattern; remove the secret and "
            "store it in an environment variable or system-managed "
            "secret store instead."
        )


def _require_string(raw: object, *, label: str, source: Path) -> str:
    if not isinstance(raw, str):
        die(f"workspace-defaults {source.as_posix()} {label} must be a string")
    return raw


def _require_bool(raw: object, *, label: str, source: Path) -> bool:
    if not isinstance(raw, bool):
        die(f"workspace-defaults {source.as_posix()} {label} must be a boolean")
    return raw


def _require_mapping(raw: object, *, label: str, source: Path) -> dict:
    if not isinstance(raw, dict):
        die(f"workspace-defaults {source.as_posix()} {label} must be a mapping")
    return raw


def _normalize_url(url: str, *, source: Path) -> str:
    if not url.startswith(("http://", "https://")):
        die(
            f"workspace-defaults {source.as_posix()} redmine.default_project.url "
            f"must be an http(s) URL: got {url!r}"
        )
    return url


def load_workspace_defaults(source: Path) -> WorkspaceDefaults:
    if not source.exists():
        die(
            f"workspace-defaults YAML not found at {source.as_posix()}; "
            "create it from the schema documented in "
            "`vibes/docs/logics/workspace-defaults-renderer.md`."
        )
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        die(
            f"workspace-defaults {source.as_posix()} root must be a YAML mapping"
        )

    _scan_for_secrets(raw, "", source=source)

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        die(
            f"workspace-defaults {source.as_posix()} schema_version must be "
            f"{SCHEMA_VERSION}, got {schema_version!r}"
        )

    redmine = _require_mapping(raw.get("redmine"), label="redmine", source=source)
    project_raw = _require_mapping(
        redmine.get("default_project"),
        label="redmine.default_project",
        source=source,
    )
    project = DefaultProject(
        identifier=_require_string(
            project_raw.get("identifier"),
            label="redmine.default_project.identifier",
            source=source,
        ),
        name=_require_string(
            project_raw.get("name"),
            label="redmine.default_project.name",
            source=source,
        ),
        url=_normalize_url(
            _require_string(
                project_raw.get("url"),
                label="redmine.default_project.url",
                source=source,
            ),
            source=source,
        ),
        parent_label=_require_string(
            project_raw.get("parent_label", ""),
            label="redmine.default_project.parent_label",
            source=source,
        ),
    )

    verification_raw = _require_mapping(
        redmine.get("verification"),
        label="redmine.verification",
        source=source,
    )
    verification = Verification(
        verified=_require_bool(
            verification_raw.get("verified"),
            label="redmine.verification.verified",
            source=source,
        ),
        verification_date=_require_string(
            verification_raw.get("verification_date", ""),
            label="redmine.verification.verification_date",
            source=source,
        ),
        verified_by=_require_string(
            verification_raw.get("verified_by", ""),
            label="redmine.verification.verified_by",
            source=source,
        ),
    )

    outputs_raw = raw.get("outputs")
    if not isinstance(outputs_raw, list) or not outputs_raw:
        die(
            f"workspace-defaults {source.as_posix()} must declare at least one output"
        )
    outputs: list[OutputSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(outputs_raw):
        item_map = _require_mapping(item, label=f"outputs[{index}]", source=source)
        kind = _require_string(
            item_map.get("kind"), label=f"outputs[{index}].kind", source=source
        )
        if kind not in KNOWN_OUTPUT_KINDS:
            die(
                f"workspace-defaults {source.as_posix()} outputs[{index}].kind "
                f"is not a supported renderer kind: {kind!r}. "
                f"Supported kinds: {sorted(KNOWN_OUTPUT_KINDS)}. Adding a new "
                f"kind requires a code change (new render function + dispatch + "
                f"tests + design-doc update), not just declaring a new target. "
                f"See vibes/docs/logics/workspace-defaults-renderer.md."
            )
        target = _require_string(
            item_map.get("target"), label=f"outputs[{index}].target", source=source
        )
        target_path = Path(target)
        if target_path.is_absolute() or ".." in target_path.parts:
            die(
                f"workspace-defaults {source.as_posix()} outputs[{index}].target "
                f"must be a repo-relative path: got {target!r}"
            )
        allowed_suffixes = KIND_ALLOWED_SUFFIXES.get(kind, frozenset())
        actual_suffix = target_path.suffix.lower()
        if allowed_suffixes and actual_suffix not in allowed_suffixes:
            die(
                f"workspace-defaults {source.as_posix()} outputs[{index}] "
                f"target {target!r} has suffix {actual_suffix or '(none)'!r}, "
                f"which kind {kind!r} cannot produce. Allowed suffixes for "
                f"this kind: {sorted(allowed_suffixes)}. Routing Markdown "
                f"content into a non-Markdown config path (e.g. "
                f"`.codex/config.toml`, `.mcd.json`) would generate invalid "
                f"config; declare a new typed kind with its own renderer "
                f"instead. See vibes/docs/logics/workspace-defaults-renderer.md."
            )
        key = target_path.as_posix()
        if key in seen:
            die(
                f"workspace-defaults {source.as_posix()} outputs[{index}].target "
                f"is duplicated: {target!r}"
            )
        seen.add(key)
        outputs.append(OutputSpec(kind=kind, target=target_path))

    return WorkspaceDefaults(
        schema_version=schema_version,
        default_project=project,
        verification=verification,
        outputs=tuple(outputs),
        source_path=source,
    )


def render_redmine_defaults_markdown(defaults: WorkspaceDefaults) -> str:
    """Render the Markdown snippet from a loaded workspace-defaults record."""
    project = defaults.default_project
    verification = defaults.verification

    parent_label_line = (
        f"- parent_label: {project.parent_label}\n"
        if project.parent_label.strip()
        else ""
    )

    header_suffix = "" if verification.is_complete else " (UNVERIFIED)"

    lines: list[str] = []
    lines.append(f"# Redmine Default Project (workspace-local){header_suffix}\n")
    lines.append("\n")
    lines.append(
        "<!-- generated by `mozyo-bridge workspace-defaults` from "
        ".mozyo-bridge/workspace-defaults.yaml -->\n"
    )
    lines.append(
        "<!-- do not edit by hand; rerun `mozyo-bridge workspace-defaults` "
        "to regenerate -->\n"
    )
    lines.append("\n")
    lines.append("## Default Project\n")
    lines.append("\n")
    lines.append(f"- identifier: `{project.identifier}`\n")
    lines.append(f"- name: {project.name}\n")
    lines.append(f"- url: {project.url}\n")
    if parent_label_line:
        lines.append(parent_label_line)
    lines.append("\n")
    lines.append("## Resolution Priority\n")
    lines.append("\n")
    lines.append(
        "1. **Explicit project id wins**. If the user instruction, ticket text, "
        "MCP request, or session context names a `project_id` / "
        "`project_identifier` / Redmine URL pointing at a specific project, "
        "use that. Do not fall back to this default when an explicit value is "
        "available.\n"
    )
    if verification.is_complete:
        lines.append(
            "2. **Verified default**. When no explicit project is named AND "
            "`verification.verified: true` in "
            "`.mozyo-bridge/workspace-defaults.yaml`, use the default "
            "identifier above. Confirm it is reachable in the current MCP / "
            "API session before creating issues "
            "(`mcp__redmine_epic_grid__get_project_structure_tool` against the "
            "identifier, or open the URL).\n"
        )
        lines.append(
            "3. **Resolution failure**. If the verified default rejects the "
            "request (missing permission, identifier renamed), do not retry "
            "silently. Escalate to the operator with the failure detail and "
            "ask for the correct project_id.\n"
        )
    else:
        lines.append(
            "2. **Default is NOT yet verified**. The verification record in "
            "`.mozyo-bridge/workspace-defaults.yaml` is incomplete (see the "
            "Verification section below). Do NOT use this default for issue "
            "creation. Ask the operator for the project_id, or escalate.\n"
        )
    lines.append("\n")
    lines.append("## Verification\n")
    lines.append("\n")
    if verification.is_complete:
        lines.append("- verified: yes\n")
        lines.append(f"- verification_date: {verification.verification_date}\n")
        lines.append(f"- verified_by: {verification.verified_by}\n")
        lines.append(
            "- After agent restart or MCP reload, re-confirm reachability "
            "before the first issue creation of the new session. The "
            "verification record is recorded once; runtime state can change "
            "between sessions.\n"
        )
    else:
        lines.append(
            "- verified: **NO** — default is unverified or the record is "
            "incomplete; treat as a suggestion only.\n"
        )
        lines.append("- Before relying on this default, verify reachability:\n")
        lines.append(
            "  1. Run `mcp__redmine_epic_grid__get_project_structure_tool` "
            "(or open the URL above) against the identifier.\n"
        )
        lines.append(
            "  2. Confirm the project is reachable from the current MCP / "
            "API session.\n"
        )
        lines.append(
            "  3. Set `verification.verified: true`, "
            "`verification.verification_date`, and `verification.verified_by` "
            "in `.mozyo-bridge/workspace-defaults.yaml`.\n"
        )
        lines.append(
            "  4. Rerun `mozyo-bridge workspace-defaults` to regenerate "
            "this snippet, then re-run `--check` to confirm clean state.\n"
        )
    lines.append("\n")
    lines.append("## Constraints\n")
    lines.append("\n")
    lines.append(
        "- This file is **generated**. Do not hand-edit. Change "
        "`.mozyo-bridge/workspace-defaults.yaml` and rerun "
        "`mozyo-bridge workspace-defaults` to regenerate.\n"
    )
    lines.append(
        "- **Do not record API keys, OAuth tokens, cookies, or personal "
        "secrets** in `.mozyo-bridge/workspace-defaults.yaml` or in this "
        "snippet. The renderer rejects credential-shape values on load. "
        "Secrets live in environment variables or system-managed secret "
        "stores.\n"
    )
    lines.append(
        "- Distributed mozyo_bridge presets / skills / docs **do not** carry "
        "project-specific identifiers. This file lives under "
        "`.mozyo-bridge/` in the target workspace and is committed to the "
        "workspace repo; it is not shipped by the mozyo-bridge package.\n"
    )
    return "".join(lines)


def resolve_input_path(repo_root: Path) -> Path:
    return (repo_root / WORKSPACE_DEFAULTS_INPUT_RELATIVE).resolve()


def resolve_output_path(repo_root: Path, target: Path) -> Path:
    if target.is_absolute():
        return target
    return (repo_root / target).resolve()


def _render_for_kind(kind: str, defaults: WorkspaceDefaults) -> str:
    """Dispatch to the typed renderer for ``kind``.

    Unknown kinds were already rejected at load time, so reaching this
    branch with an unknown kind would indicate a code-side regression
    (forgot to add the dispatch arm after adding to KNOWN_OUTPUT_KINDS).
    Fail loudly rather than silently writing nothing.
    """
    if kind == KIND_REDMINE_MARKDOWN:
        return render_redmine_defaults_markdown(defaults)
    die(
        f"workspace-defaults renderer is missing a dispatch arm for "
        f"output kind {kind!r}; add the typed render in "
        f"`_render_for_kind` before re-declaring the kind as supported."
    )


def collect_render_results(repo_root: Path) -> list[RenderResult]:
    defaults = load_workspace_defaults(resolve_input_path(repo_root))
    results: list[RenderResult] = []
    for output in defaults.outputs:
        output_path = resolve_output_path(repo_root, output.target)
        rendered = _render_for_kind(output.kind, defaults)
        on_disk = (
            output_path.read_text(encoding="utf-8") if output_path.exists() else None
        )
        results.append(
            RenderResult(output_path=output_path, rendered=rendered, on_disk=on_disk)
        )
    return results


def write_render_results(results: list[RenderResult]) -> list[Path]:
    written: list[Path] = []
    for result in results:
        result.output_path.parent.mkdir(parents=True, exist_ok=True)
        result.output_path.write_text(result.rendered, encoding="utf-8")
        written.append(result.output_path)
    return written
