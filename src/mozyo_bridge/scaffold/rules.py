from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePath
from string import Template
from time import strftime

import yaml

from mozyo_bridge import __version__
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import mozyo_bridge_home

ROUTER_TEMPLATE_PRESET = "_router"
RULE_RELATIVE_PATH = Path("rules") / "presets"
REPO_LOCAL_DIRNAME = ".mozyo-bridge"
MANIFEST_RELATIVE_PATH = Path(REPO_LOCAL_DIRNAME) / "scaffold.json"
PRESET_REGISTRY_FILENAME = "presets.yaml"

# Subdirectory inside a preset package that holds repo-local artifacts
# the scaffold should copy verbatim into the target repository in
# addition to the router pair. Used by full-governance presets that ship
# rule / tool / catalog skeleton files alongside the workflow document.
PRESET_FILES_DIRNAME = "files"

# Filesystem cruft inside a preset's ``files/`` subtree that must NOT be
# copied to the target repo. `pip install` may write `__pycache__/`
# entries next to any shipped `.py` source file (the governed preset
# ships several catalog tools), and reading a `.pyc` as UTF-8 text
# crashes the walker. These names / suffixes are dropped at walk time
# regardless of how the preset directory ended up on disk.
PRESET_FILES_SKIP_DIRS = frozenset({"__pycache__"})
PRESET_FILES_SKIP_SUFFIXES = frozenset({".pyc", ".pyo"})

# Category → repo-relative POSIX path *prefix* mapping that slices a
# governed preset's repo-local artifacts. Any rendered file whose
# relative path starts with one of the prefixes belongs to that
# category; categories are switched on/off as a group, and the manifest
# automatically tracks only what was actually written.
#
# Categories come in two flavours:
#   * opt-out (default-on): shipped unless the operator passes
#     `--skip-<category>`. The CLI wires the flag into `skip_categories`.
#   * opt-in (default-off): shipped only when the operator passes the
#     matching `--with-<category>` flag, wired into `with_categories`.
# Adding a category here plus its flag in the CLI is the entire
# integration point.
PRESET_FILES_CATEGORY_PREFIXES: dict[str, str] = {
    "tmux-ui": ".mozyo-bridge/tmux/",
    "nagger": ".claude-nagger/",
    "worktree-runbook": "vibes/docs/logics/",
    "sublane-flow": "vibes/docs/profiles/",
}
PRESET_FILES_CATEGORIES = frozenset(PRESET_FILES_CATEGORY_PREFIXES.keys())

# Opt-in categories ship only when explicitly enabled (default-off).
# They are skipped from the rendered set unless their label is present
# in the `with_categories` opt-in set, so a plain `scaffold apply` never
# installs them. The worktree/sublane runbook docs are distributed this
# way (Redmine #11955) because adoption is per-project and the docs are
# generic, public-safe operator recipes rather than always-on guardrail
# artifacts.
#
# `sublane-flow` (Redmine #12362 / #12363) is the second opt-in category.
# Beyond shipping its portable profile doc under `vibes/docs/profiles/`,
# enabling it also activates a thin sublane-flow read-route in the
# generated routers (see ``SUBLANE_FLOW_CATEGORY`` and
# ``render_router_pair``). A plain apply keeps sublane flow out of every
# runtime-active entrypoint; the route appears only on opt-in.
PRESET_FILES_OPT_IN_CATEGORIES = frozenset({"worktree-runbook", "sublane-flow"})

# The opt-in category that, in addition to shipping docs, toggles the
# router read-route variant. Kept as a named constant so the router
# selector and the CLI flag wiring agree on the exact label.
SUBLANE_FLOW_CATEGORY = "sublane-flow"

# Opt-out categories are everything that is not opt-in: default-on
# artifacts an operator can drop with `--skip-<category>`.
PRESET_FILES_OPT_OUT_CATEGORIES = PRESET_FILES_CATEGORIES - PRESET_FILES_OPT_IN_CATEGORIES

CENTRAL_MODE = "central"
REPO_LOCAL_MODE = "repo-local"
VALID_MODES = frozenset({CENTRAL_MODE, REPO_LOCAL_MODE})

# Portable expression for the mozyo-bridge home root. Carries the
# ${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge} fallback verbatim so committed
# documents (routers, READMEs, generator output) never leak the operator's
# resolved $HOME.
PORTABLE_HOME_EXPRESSION = "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}"

# Portable rule_path templates embedded into generated routers and the
# manifest. Both forms must stay host-independent so the artifact can be
# committed without leaking the operator's $HOME. Central mode keeps the
# existing ${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge} expansion contract that
# consuming agents already understand. Repo-local mode uses a path that is
# always relative to the target repo root — AGENTS.md / CLAUDE.md sit at
# the repo root so a leading `.mozyo-bridge/...` resolves correctly from
# the agent's cwd without any expansion at all, which is exactly what
# Dev Container / ephemeral-home workspaces need.
PORTABLE_RULE_PATH_CENTRAL_TEMPLATE = (
    "${{MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}}/rules/presets/{preset}/agent-workflow.md"
)
PORTABLE_RULE_PATH_REPO_LOCAL_TEMPLATE = (
    f"{REPO_LOCAL_DIRNAME}/rules/presets/{{preset}}/agent-workflow.md"
)

# Marker pair used to delimit a project-local additions block inside scaffold-
# generated routers (AGENTS.md / CLAUDE.md). Content between these markers is
# preserved by `scaffold apply` / `scaffold diff` when the on-disk router
# carries the pair, so re-syncing the scaffold base does not erase project-
# local additions the operator put inside the block. The markers are HTML
# comments so they render invisible in Markdown. Eligibility is per-file:
# only AGENTS.md and CLAUDE.md preserve; the manifest does not. Preservation
# requires BOTH the rendered template and the on-disk file to carry the pair;
# legacy on-disk files without markers fall through to the existing overwrite
# / backup / force behavior unchanged.
PROJECT_LOCAL_BEGIN_MARKER = "<!-- mozyo-bridge:project-local-additions:begin -->"
PROJECT_LOCAL_END_MARKER = "<!-- mozyo-bridge:project-local-additions:end -->"
PROJECT_LOCAL_PRESERVED_FILENAMES = frozenset({"AGENTS.md", "CLAUDE.md"})


@dataclass(frozen=True)
class RenderedFile:
    path: Path
    content: str


@dataclass(frozen=True)
class PresetDefinition:
    name: str
    workflow: str
    ticket_anchor_label: str
    extends: str | None = None


def _registry_text() -> str:
    return (
        resources.files("mozyo_bridge.scaffold.presets")
        .joinpath(PRESET_REGISTRY_FILENAME)
        .read_text(encoding="utf-8")
    )


def _load_preset_registry() -> dict[str, PresetDefinition]:
    raw = yaml.safe_load(_registry_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("presets"), dict):
        die(f"{PRESET_REGISTRY_FILENAME} must contain a mapping named `presets`")
    definitions: dict[str, PresetDefinition] = {}
    for name, value in raw["presets"].items():
        if not isinstance(name, str) or not name:
            die(f"{PRESET_REGISTRY_FILENAME} contains an invalid preset name: {name!r}")
        if not isinstance(value, dict):
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} must be a mapping")
        workflow = value.get("workflow")
        ticket_anchor_label = value.get("ticket_anchor_label")
        extends = value.get("extends")
        if not isinstance(workflow, str) or not workflow.endswith("/agent-workflow.md"):
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} has invalid workflow")
        if not isinstance(ticket_anchor_label, str) or not ticket_anchor_label:
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} has invalid ticket_anchor_label")
        if extends is not None and not isinstance(extends, str):
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} has invalid extends")
        workflow_preset = workflow.split("/", 1)[0]
        if workflow_preset != name:
            die(
                f"{PRESET_REGISTRY_FILENAME} preset {name!r} workflow must live under "
                f"{name!r}, got {workflow!r}"
            )
        definitions[name] = PresetDefinition(
            name=name,
            workflow=workflow,
            ticket_anchor_label=ticket_anchor_label,
            extends=extends,
        )
    for definition in definitions.values():
        if definition.extends is not None and definition.extends not in definitions:
            die(
                f"{PRESET_REGISTRY_FILENAME} preset {definition.name!r} extends "
                f"unknown preset {definition.extends!r}"
            )
    return definitions


PRESET_DEFINITIONS = _load_preset_registry()
PRESETS = tuple(PRESET_DEFINITIONS)


def preset_definition(preset: str) -> PresetDefinition:
    try:
        return PRESET_DEFINITIONS[preset]
    except KeyError:
        die(f"unsupported rules preset: {preset}")


# NOTE: `mozyo_bridge_home()` moved to `mozyo_bridge.shared.paths` (Redmine
# #11429) so the workspace registry shares the exact same home contract.
# It stays importable from this module for existing callers.

@dataclass(frozen=True)
class RulesStore:
    """Resolved location of a rules store on disk.

    A rules store carries the installed central preset(s) for one or more
    presets, regardless of whether it lives under the user's mozyo-bridge
    home (central mode) or under a target repo's ``.mozyo-bridge``
    directory (repo-local mode).

    Attributes:
        root: Absolute path under which ``rules/presets/<preset>/`` lives.
            For central mode this is the mozyo-bridge home; for repo-local
            mode this is ``<repo>/.mozyo-bridge``.
        mode: ``"central"`` or ``"repo-local"``.
        repo: For repo-local mode, the absolute path to the target repo
            root. ``None`` in central mode.
    """

    root: Path
    mode: str
    repo: Path | None = None

    @property
    def is_repo_local(self) -> bool:
        return self.mode == REPO_LOCAL_MODE


def resolve_rules_store(
    *,
    home: Path | str | None = None,
    repo_local: Path | str | None = None,
) -> RulesStore:
    """Build a ``RulesStore`` from raw CLI inputs.

    ``--home`` and ``--repo-local`` are mutually exclusive; passing both
    is a deterministic operator error so the helper dies before any
    filesystem work happens. When neither is given, defaults to central
    mode using ``MOZYO_BRIDGE_HOME`` (or ``~/.mozyo_bridge``) so existing
    callers keep their pre-repo-local behavior unchanged.
    """
    if home is not None and repo_local is not None:
        die("--home and --repo-local are mutually exclusive")
    if repo_local is not None:
        repo_root = Path(repo_local).expanduser().resolve()
        return RulesStore(
            root=repo_root / REPO_LOCAL_DIRNAME,
            mode=REPO_LOCAL_MODE,
            repo=repo_root,
        )
    if home is not None:
        root = Path(home).expanduser().resolve()
    else:
        root = mozyo_bridge_home()
    return RulesStore(root=root, mode=CENTRAL_MODE, repo=None)


def _coerce_store(
    store: RulesStore | None,
    home: Path | str | None,
) -> RulesStore:
    """Back-compat coercion: prefer ``store`` when supplied, else build a
    central store from ``home``. Keeps the pre-repo-local positional/keyword
    ``home=...`` API working for tests and external callers."""
    if store is not None:
        return store
    return resolve_rules_store(home=home)


def package_preset_root(preset: str):
    preset_definition(preset)
    return resources.files("mozyo_bridge.scaffold.presets").joinpath(preset)


def router_template_text(filename: str) -> str:
    return (
        resources.files("mozyo_bridge.scaffold.presets")
        .joinpath(ROUTER_TEMPLATE_PRESET)
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def router_template_filename(output_filename: str, *, sublane_flow: bool) -> str:
    """Map a router output filename to the template variant to read.

    The base apply reads ``AGENTS.md`` / ``CLAUDE.md`` from ``_router/``.
    When the ``--with-sublane-flow`` opt-in is enabled the scaffold reads
    the ``AGENTS.sublane.md`` / ``CLAUDE.sublane.md`` variants instead —
    these carry the extra read-route section — while still writing the
    result to the target as ``AGENTS.md`` / ``CLAUDE.md``. Both variants
    are canonical-rendered from the same source so they cannot drift.
    """
    if not sublane_flow:
        return output_filename
    stem, _, suffix = output_filename.rpartition(".")
    return f"{stem}.sublane.{suffix}"


def installed_preset_dir(
    preset: str,
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> Path:
    resolved = _coerce_store(store, home)
    return resolved.root / RULE_RELATIVE_PATH / preset


def installed_agent_workflow(
    preset: str,
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> Path:
    return installed_preset_dir(preset, home, store=store) / "agent-workflow.md"


def preset_install_requirements(preset: str) -> tuple[str, ...]:
    seen: list[str] = []
    current: str | None = preset
    while current is not None:
        if current in seen:
            die(f"preset inheritance cycle detected: {' -> '.join(seen + [current])}")
        definition = preset_definition(current)
        seen.append(current)
        current = definition.extends
    return tuple(seen)


def portable_rule_path(preset: str, *, repo_local: bool = False) -> str:
    """Return the symbolic rule_path embedded into routers and the manifest.

    Both forms are host-independent: ``central`` uses
    ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}`` which the consuming agent
    expands at read time; ``repo-local`` uses a relative path that
    resolves against the target repo root so Dev Container / ephemeral
    workspaces can carry the preset without depending on the operator's
    $HOME at runtime.
    """
    preset_definition(preset)
    template = (
        PORTABLE_RULE_PATH_REPO_LOCAL_TEMPLATE
        if repo_local
        else PORTABLE_RULE_PATH_CENTRAL_TEMPLATE
    )
    return template.format(preset=preset)


def package_text(preset: str, filename: str) -> str:
    return package_preset_root(preset).joinpath(filename).read_text(encoding="utf-8")


def package_version(preset: str) -> str:
    return package_text(preset, "VERSION").strip()


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_rules(
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> list[Path]:
    resolved = _coerce_store(store, home)
    written: list[Path] = []
    for preset in PRESETS:
        target_dir = installed_preset_dir(preset, store=resolved)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("VERSION", "agent-workflow.md"):
            content = package_text(preset, filename)
            target = target_dir / filename
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8")
                written.append(target)
    return written


def rules_status(
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> list[dict[str, str]]:
    resolved = _coerce_store(store, home)
    rows: list[dict[str, str]] = []
    for preset in PRESETS:
        expected_version = package_version(preset)
        preset_dir = installed_preset_dir(preset, store=resolved)
        version_path = preset_dir / "VERSION"
        workflow_path = preset_dir / "agent-workflow.md"
        if not version_path.exists() or not workflow_path.exists():
            status = "missing"
            installed_version = "-"
        else:
            installed_version = version_path.read_text(encoding="utf-8").strip()
            status = "ok" if installed_version == expected_version else "outdated"
        rows.append(
            {
                "preset": preset,
                "status": status,
                "installed": installed_version,
                "packaged": expected_version,
                "path": str(workflow_path),
            }
        )
    return rows


def require_installed_preset(
    preset: str,
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> Path:
    resolved = _coerce_store(store, home)
    missing: list[str] = []
    for required_preset in preset_install_requirements(preset):
        workflow = installed_agent_workflow(required_preset, store=resolved)
        version = installed_preset_dir(required_preset, store=resolved) / "VERSION"
        if not workflow.exists() or not version.exists():
            missing.append(required_preset)
    if missing:
        hint = (
            "mozyo-bridge rules install --repo-local "
            + str(resolved.repo)
            if resolved.is_repo_local and resolved.repo is not None
            else "mozyo-bridge rules install"
        )
        die(
            "rules preset is not installed: "
            + ", ".join(missing)
            + f". Run `{hint}` first."
        )
    return installed_agent_workflow(preset, store=resolved)


def router_context(
    preset: str,
    target: Path,
    workflow_path: Path,
    *,
    repo_local: bool = False,
) -> dict[str, str]:
    definition = preset_definition(preset)
    return {
        "preset": preset,
        "preset_version": package_version(preset),
        "project_root": str(target),
        "rule_path": portable_rule_path(preset, repo_local=repo_local),
        "mozyo_bridge_version": __version__,
        "ticket_anchor_label": definition.ticket_anchor_label,
    }


def render_router_pair(
    preset: str,
    target: Path,
    workflow_path: Path,
    *,
    repo_local: bool = False,
    sublane_flow: bool = False,
) -> list[RenderedFile]:
    context = router_context(preset, target, workflow_path, repo_local=repo_local)
    files = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        template_filename = router_template_filename(filename, sublane_flow=sublane_flow)
        template = Template(router_template_text(template_filename))
        files.append(RenderedFile(Path(filename), template.safe_substitute(context)))
    return files


def _walk_preset_files_dir(files_root) -> list[tuple[Path, str]]:
    """Walk a preset's ``files/`` subdirectory and return (relative_path, content) pairs.

    The walker uses ``importlib.resources`` semantics so the preset package
    works whether the source tree is on disk or installed from a wheel.
    ``files_root`` is a ``Traversable`` rooted at the preset's ``files``
    directory; relative paths returned are POSIX-style strings rooted at
    that directory.
    """
    collected: list[tuple[Path, str]] = []

    def visit(traversable, relative: PurePath) -> None:
        for child in traversable.iterdir():
            child_rel = relative / child.name
            if child.is_dir():
                # Skip bytecode cache directories that pip can create
                # next to shipped `.py` files; copying them into the
                # target repo crashes the walker because `.pyc` is not
                # UTF-8 text. See PRESET_FILES_SKIP_DIRS.
                if child.name in PRESET_FILES_SKIP_DIRS:
                    continue
                visit(child, child_rel)
                continue
            if Path(child.name).suffix in PRESET_FILES_SKIP_SUFFIXES:
                continue
            content = child.read_text(encoding="utf-8")
            collected.append((Path(child_rel.as_posix()), content))

    if not files_root.is_dir():
        return collected
    visit(files_root, PurePath(""))
    return sorted(collected, key=lambda item: item[0].as_posix())


def _normalize_skip_categories(skip_categories: set[str] | None) -> frozenset[str]:
    """Validate and freeze the operator-supplied opt-out skip category set.

    Only opt-out (default-on) categories are valid skip targets. Passing
    an opt-in category here is an operator error — opt-in categories are
    disabled by default and enabled via ``with_categories``, not skipped.
    Unknown labels are an operator error, not a silent miss — the CLI
    layer raises before we reach here, but a library caller that bypasses
    the CLI will also be told. The empty / None case is the common
    default-on path.
    """
    if not skip_categories:
        return frozenset()
    unknown = set(skip_categories) - PRESET_FILES_OPT_OUT_CATEGORIES
    if unknown:
        die(
            "unknown scaffold skip categories: "
            + ", ".join(sorted(unknown))
            + f"; expected one of {sorted(PRESET_FILES_OPT_OUT_CATEGORIES)}"
        )
    return frozenset(skip_categories)


def _normalize_with_categories(with_categories: set[str] | None) -> frozenset[str]:
    """Validate and freeze the operator-supplied opt-in category set.

    Only opt-in (default-off) categories are valid ``--with-*`` targets.
    Passing an opt-out / unknown label is an operator error. The empty /
    None case is the common default-off path (nothing opted in).
    """
    if not with_categories:
        return frozenset()
    unknown = set(with_categories) - PRESET_FILES_OPT_IN_CATEGORIES
    if unknown:
        die(
            "unknown scaffold opt-in categories: "
            + ", ".join(sorted(unknown))
            + f"; expected one of {sorted(PRESET_FILES_OPT_IN_CATEGORIES)}"
        )
    return frozenset(with_categories)


def _effective_skip_categories(
    skip_categories: set[str] | None,
    with_categories: set[str] | None,
) -> frozenset[str]:
    """Resolve the final set of categories to drop from the rendered files.

    The effective skip set is the union of:
      * explicit opt-out labels (``--skip-<category>``), and
      * every opt-in category that was NOT enabled via ``--with-<category>``.
    This makes opt-in categories default-off: a plain apply skips them,
    and they ship only when their label is present in ``with_categories``.
    """
    skip = _normalize_skip_categories(skip_categories)
    enabled = _normalize_with_categories(with_categories)
    default_skipped = PRESET_FILES_OPT_IN_CATEGORIES - enabled
    return frozenset(skip | default_skipped)


def sublane_flow_enabled(with_categories: set[str] | None) -> bool:
    """Return True when the ``sublane-flow`` opt-in category is enabled.

    Reuses ``_normalize_with_categories`` so an unknown / opt-out label
    fails the same way it does for the file-shipping path, and so the
    router-route toggle and the doc-shipping toggle key off one validated
    set. The empty / None case is the default-off path (no route added).
    """
    return SUBLANE_FLOW_CATEGORY in _normalize_with_categories(with_categories)


def render_preset_extra_files(
    preset: str,
    *,
    skip_categories: set[str] | None = None,
    with_categories: set[str] | None = None,
) -> list[RenderedFile]:
    """Return the preset's repo-local artifacts as :class:`RenderedFile`.

    Returns an empty list when the preset does not ship a ``files/`` subtree.
    The returned paths are repo-relative (e.g. ``.mozyo-bridge/rules/...``)
    so callers can write them directly under the scaffold target.

    ``skip_categories`` drops every artifact whose repo-relative path
    starts with an opt-out category's registered prefix (see
    ``PRESET_FILES_CATEGORY_PREFIXES``). ``with_categories`` enables an
    opt-in (default-off) category so its artifacts are included; opt-in
    categories are otherwise skipped. Dropped entries do not appear in
    the manifest either, so ``scaffold status`` stays clean after a
    partial apply.
    """
    preset_definition(preset)
    skip = _effective_skip_categories(skip_categories, with_categories)
    skip_prefixes = tuple(
        PRESET_FILES_CATEGORY_PREFIXES[name] for name in skip
    )
    files_root = package_preset_root(preset).joinpath(PRESET_FILES_DIRNAME)
    rendered: list[RenderedFile] = []
    for relative, content in _walk_preset_files_dir(files_root):
        if skip_prefixes and relative.as_posix().startswith(skip_prefixes):
            continue
        rendered.append(RenderedFile(relative, content))
    return rendered


def installed_preset_hash(
    preset: str,
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> str | None:
    workflow = installed_agent_workflow(preset, home, store=store)
    if not workflow.exists():
        return None
    return sha256_file(workflow)


def installed_preset_version(
    preset: str,
    home: Path | None = None,
    *,
    store: RulesStore | None = None,
) -> str | None:
    version_path = installed_preset_dir(preset, home, store=store) / "VERSION"
    if not version_path.exists():
        return None
    return version_path.read_text(encoding="utf-8").strip()


def manifest_content(
    preset: str,
    workflow_path: Path,
    rendered: list[RenderedFile],
    *,
    mode: str = CENTRAL_MODE,
) -> str:
    # `rule_path` is stored in symbolic form so the manifest stays safe to commit
    # in target repositories. Drift detection uses `preset_hash` and per-file
    # sha256 entries, not this field, so sanitizing it does not weaken status.
    if mode not in VALID_MODES:
        die(f"unsupported manifest mode: {mode!r}; expected one of {sorted(VALID_MODES)}")
    repo_local = mode == REPO_LOCAL_MODE
    payload = {
        "schema_version": 2,
        "mode": mode,
        "preset": preset,
        "preset_version": package_version(preset),
        "preset_hash": sha256_file(workflow_path),
        "generated_by": f"mozyo-bridge {__version__}",
        "rule_path": portable_rule_path(preset, repo_local=repo_local),
        "files": {
            str(item.path): {
                "sha256": sha256_text(item.content),
            }
            for item in rendered
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak.{strftime('%Y%m%d%H%M%S')}")


def extract_project_local_block(text: str) -> str | None:
    """Return the literal content between project-local markers, or None.

    Returns None when either marker is absent so callers can fall through to
    the existing overwrite-or-protect behavior unchanged. The returned string
    is the raw inner content including leading/trailing whitespace so
    substitution is byte-for-byte stable.
    """
    begin = text.find(PROJECT_LOCAL_BEGIN_MARKER)
    if begin < 0:
        return None
    block_start = begin + len(PROJECT_LOCAL_BEGIN_MARKER)
    end = text.find(PROJECT_LOCAL_END_MARKER, block_start)
    if end < 0:
        return None
    return text[block_start:end]


def substitute_project_local_block(rendered: str, replacement: str) -> str:
    """Replace the project-local block in `rendered` with `replacement`.

    Returns `rendered` unchanged if either marker is missing from the
    rendered template (defensive fallback — preservation requires both sides
    to carry the marker pair).
    """
    begin = rendered.find(PROJECT_LOCAL_BEGIN_MARKER)
    if begin < 0:
        return rendered
    block_start = begin + len(PROJECT_LOCAL_BEGIN_MARKER)
    end = rendered.find(PROJECT_LOCAL_END_MARKER, block_start)
    if end < 0:
        return rendered
    return rendered[:block_start] + replacement + rendered[end:]


def apply_project_local_preservation(
    rendered_items: list[RenderedFile],
    target: Path,
) -> list[RenderedFile]:
    """Substitute on-disk project-local blocks into the rendered routers.

    For each rendered file whose basename is in PROJECT_LOCAL_PRESERVED_FILENAMES,
    if the on-disk file exists and contains the marker pair, the content between
    the markers in the on-disk file is substituted into the rendered template
    (which also carries the marker pair). The manifest must be built AFTER this
    step so its per-file sha256 entries reflect the post-substitution content
    that will land on disk.

    Files not eligible for preservation, files missing on disk, and on-disk
    files without the marker pair all pass through unchanged.
    """
    preserved: list[RenderedFile] = []
    for item in rendered_items:
        if item.path.name not in PROJECT_LOCAL_PRESERVED_FILENAMES:
            preserved.append(item)
            continue
        on_disk_path = target / item.path
        if not on_disk_path.exists():
            preserved.append(item)
            continue
        try:
            on_disk_text = on_disk_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            preserved.append(item)
            continue
        block = extract_project_local_block(on_disk_text)
        if block is None:
            preserved.append(item)
            continue
        preserved.append(RenderedFile(item.path, substitute_project_local_block(item.content, block)))
    return preserved


def _resolve_scaffold_store(
    target: Path,
    home: Path | None,
    repo_local: bool,
) -> RulesStore:
    """Build the rules store used by scaffold render/write paths.

    ``repo_local=True`` forces a repo-local store rooted at ``<target>/.mozyo-bridge``;
    in that case ``home`` must be ``None`` or the call dies (the CLI surface
    rejects the conflict before we get here, but the assertion is duplicated
    in-library so library callers cannot bypass it).
    """
    if repo_local and home is not None:
        die("--home and --repo-local are mutually exclusive")
    if repo_local:
        return resolve_rules_store(repo_local=target)
    return resolve_rules_store(home=home)


def render_scaffold_files(
    preset: str,
    target: Path,
    home: Path | None = None,
    *,
    repo_local: bool = False,
    skip_categories: set[str] | None = None,
    with_categories: set[str] | None = None,
) -> list[RenderedFile]:
    target = target.expanduser().resolve()
    store = _resolve_scaffold_store(target, home, repo_local)
    workflow_path = require_installed_preset(preset, store=store)
    rendered = render_router_pair(
        preset,
        target,
        workflow_path,
        repo_local=store.is_repo_local,
        sublane_flow=sublane_flow_enabled(with_categories),
    )
    rendered = apply_project_local_preservation(rendered, target)
    extras = render_preset_extra_files(
        preset, skip_categories=skip_categories, with_categories=with_categories
    )
    manifest_inputs = rendered + extras
    manifest = RenderedFile(
        MANIFEST_RELATIVE_PATH,
        manifest_content(preset, workflow_path, manifest_inputs, mode=store.mode),
    )
    return rendered + extras + [manifest]


def _previously_tracked_relative_paths(target: Path) -> set[str]:
    """Return repo-relative POSIX paths the prior scaffold manifest tracked.

    Returns an empty set when no manifest exists, when the manifest is
    unreadable, or when the manifest's ``files`` entry is malformed. The
    caller treats missing prior state as a fresh apply (no outgoing
    files to reconcile).
    """
    try:
        state = scaffold_state(target)
    except (OSError, json.JSONDecodeError):
        return set()
    if not state:
        return set()
    files = state.get("files") if isinstance(state, dict) else None
    if not isinstance(files, dict):
        return set()
    return {name for name in files.keys() if isinstance(name, str)}


def write_scaffold(
    preset: str,
    target: Path,
    dry_run: bool = False,
    backup: bool = False,
    force: bool = False,
    home: Path | None = None,
    *,
    repo_local: bool = False,
    skip_categories: set[str] | None = None,
    with_categories: set[str] | None = None,
) -> list[Path]:
    if backup and force:
        die("--backup and --force are mutually exclusive")
    target = target.expanduser().resolve()
    store = _resolve_scaffold_store(target, home, repo_local)
    workflow_path = require_installed_preset(preset, store=store)
    rendered = render_router_pair(
        preset,
        target,
        workflow_path,
        repo_local=store.is_repo_local,
        sublane_flow=sublane_flow_enabled(with_categories),
    )
    rendered = apply_project_local_preservation(rendered, target)
    extras = render_preset_extra_files(
        preset, skip_categories=skip_categories, with_categories=with_categories
    )
    manifest_inputs = rendered + extras
    manifest = RenderedFile(
        MANIFEST_RELATIVE_PATH,
        manifest_content(preset, workflow_path, manifest_inputs, mode=store.mode),
    )
    all_files = rendered + extras + [manifest]

    # Reconcile "outgoing" files: entries the previous manifest tracked
    # that the current render no longer claims. These typically come
    # from operator opt-outs (`--skip-*`) or upstream preset removals.
    # Leaving them on disk lets `scaffold status` falsely report `clean`
    # because the new manifest never claimed them — opt-out and drift
    # visibility silently diverge. We treat outgoing files like a
    # destructive write so `--backup` / `--force` gate the removal.
    rendered_rel = {item.path.as_posix() for item in rendered + extras}
    previously_tracked = _previously_tracked_relative_paths(target)
    outgoing_rel = previously_tracked - rendered_rel
    outgoing_existing = [
        target / relative
        for relative in sorted(outgoing_rel)
        if (target / relative).exists()
    ]

    existing = [target / item.path for item in all_files if (target / item.path).exists()]
    # Routers (AGENTS.md / CLAUDE.md) and every preset-shipped extra are
    # protected from silent overwrite. Only the scaffold's own manifest can
    # be safely overwritten because it tracks the scaffold's recorded state.
    protected_paths = {target / item.path for item in rendered + extras}
    protected = [path for path in existing if path in protected_paths]
    protected.extend(outgoing_existing)
    if protected and not backup and not force:
        die("refusing to overwrite existing scaffold files: " + ", ".join(str(path) for path in protected))
    if dry_run:
        return [target / item.path for item in all_files]
    if backup:
        for path in existing:
            backup_target = backup_path(path)
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_target)
        for path in outgoing_existing:
            backup_target = backup_path(path)
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_target)
    for item in all_files:
        path = target / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content, encoding="utf-8")
    # Remove outgoing files after the new manifest is in place so a
    # crash mid-apply still leaves a coherent on-disk state (worst case:
    # both old artifact and new manifest reference it, which the next
    # apply will reconcile). Skip files we just wrote — `outgoing_rel`
    # already excludes them via set subtraction, but the guard is cheap.
    for path in outgoing_existing:
        if path in protected_paths:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        # Best-effort directory cleanup: remove now-empty parents up to
        # the target root so opt-outs don't leave empty `.claude-nagger/`
        # or `.mozyo-bridge/tmux/` directories behind.
        parent = path.parent
        while parent != target and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return [target / item.path for item in all_files]


def scaffold_state(target: Path) -> dict[str, object] | None:
    path = target / MANIFEST_RELATIVE_PATH
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def scaffold_status(target: Path, home: Path | None = None) -> dict[str, object]:
    target = target.expanduser().resolve()
    manifest_path = target / MANIFEST_RELATIVE_PATH
    result: dict[str, object] = {
        "target": str(target),
        "manifest_path": str(manifest_path),
        "manifest": "missing",
        "clean": False,
    }
    if not manifest_path.exists():
        return result

    try:
        state = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result["manifest"] = "invalid"
        result["error"] = f"manifest is not valid JSON: {exc}"
        return result
    if not isinstance(state, dict):
        result["manifest"] = "invalid"
        result["error"] = "manifest root must be a JSON object"
        return result
    preset = state.get("preset")
    if not isinstance(preset, str) or preset not in PRESET_DEFINITIONS:
        result["manifest"] = "invalid"
        result["error"] = f"manifest preset is missing or unsupported: {preset!r}"
        return result

    manifest_mode = state.get("mode", CENTRAL_MODE)
    if manifest_mode not in VALID_MODES:
        result["manifest"] = "invalid"
        result["error"] = (
            f"manifest mode is unsupported: {manifest_mode!r}; "
            f"expected one of {sorted(VALID_MODES)}"
        )
        return result

    # Repo-local manifests draw their central preset from the target repo's
    # own .mozyo-bridge store, not the operator's mozyo-bridge home. Passing
    # ``--home`` against a repo-local manifest is a sign of operator
    # confusion (the home is unused for that target), so refuse rather than
    # silently comparing against the wrong store.
    if manifest_mode == REPO_LOCAL_MODE and home is not None:
        result["manifest"] = "invalid"
        result["error"] = (
            "manifest is in repo-local mode; --home is unused here. Rerun "
            "without --home, or regenerate the manifest in central mode."
        )
        return result

    if manifest_mode == REPO_LOCAL_MODE:
        store = resolve_rules_store(repo_local=target)
    else:
        store = resolve_rules_store(home=home)

    schema_version = state.get("schema_version")
    manifest_preset_version = state.get("preset_version")
    manifest_preset_hash = state.get("preset_hash")
    manifest_files = state.get("files") if isinstance(state.get("files"), dict) else {}

    # Schema v2 contract: `files` must include AGENTS.md and CLAUDE.md with sha256
    # strings so router drift can actually be verified. Without these, the status
    # command cannot answer its own question, so refuse to call the manifest
    # usable. Schema v1 manifests predate this contract and stay tolerated
    # (they fall through to the `manifest-missing-hash` per-file status below).
    if schema_version == 2:
        required_router_files = ("AGENTS.md", "CLAUDE.md")
        missing_router_entries = []
        for filename in required_router_files:
            entry = manifest_files.get(filename)
            if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
                missing_router_entries.append(filename)
        if missing_router_entries:
            result["manifest"] = "invalid"
            result["error"] = (
                "schema v2 manifest is missing router hash entries for: "
                + ", ".join(missing_router_entries)
                + ". Regenerate with `mozyo-bridge scaffold apply <preset> --backup`."
            )
            return result

    central_workflow = installed_agent_workflow(preset, store=store)
    central_version = installed_preset_version(preset, store=store)
    central_hash = installed_preset_hash(preset, store=store)
    missing_requirements = [
        required_preset
        for required_preset in preset_install_requirements(preset)
        if installed_preset_hash(required_preset, store=store) is None
        or installed_preset_version(required_preset, store=store) is None
    ]
    if missing_requirements:
        central_status = "missing"
    elif manifest_preset_hash is None:
        # Pre-v2 manifest cannot detect content drift. Fall back to version compare.
        if manifest_preset_version is not None and manifest_preset_version == central_version:
            central_status = "ok-version-only"
        else:
            central_status = "drifted-version"
    elif manifest_preset_hash != central_hash:
        central_status = "drifted-content"
    elif manifest_preset_version is not None and manifest_preset_version != central_version:
        # Same content but the recorded version label moved (rare).
        central_status = "drifted-version"
    else:
        central_status = "ok"

    file_rows: list[dict[str, str]] = []
    for filename in sorted(manifest_files.keys()):
        recorded = manifest_files.get(filename)
        expected_hash = (
            recorded.get("sha256") if isinstance(recorded, dict) else None
        )
        on_disk = target / filename
        if not on_disk.exists():
            file_status = "missing"
            on_disk_hash = None
        elif not isinstance(expected_hash, str):
            file_status = "manifest-missing-hash"
            on_disk_hash = sha256_file(on_disk)
        else:
            on_disk_hash = sha256_file(on_disk)
            file_status = "ok" if on_disk_hash == expected_hash else "drifted"
        file_rows.append(
            {
                "path": filename,
                "status": file_status,
                "expected_sha256": expected_hash or "",
                "actual_sha256": on_disk_hash or "",
            }
        )

    # Only an exact `ok` is fully clean. `ok-version-only` means the manifest is
    # schema v1 and we cannot detect content drift; flag so the user regenerates
    # the manifest under schema v2.
    central_drift = central_status != "ok"
    router_drift = any(row["status"] != "ok" for row in file_rows)
    clean = not central_drift and not router_drift

    result.update(
        {
            "manifest": "present",
            "schema_version": schema_version,
            "mode": manifest_mode,
            "preset": preset,
            "rule_path": str(central_workflow),
            "manifest_preset_version": manifest_preset_version,
            "manifest_preset_hash": manifest_preset_hash,
            "installed_preset_version": central_version,
            "installed_preset_hash": central_hash,
            "central_status": central_status,
            "files": file_rows,
            "clean": clean,
        }
    )
    return result
