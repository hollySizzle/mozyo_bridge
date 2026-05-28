"""Canonical single-source conditional renderer for guardrail outputs.

The canonical source is a YAML file under
``src/mozyo_bridge/scaffold/canonical_sources/<id>.yaml`` plus its
adjacent ``<id>/bodies/*.md`` body files. The renderer concatenates
fragment bodies whose ``when`` clause matches each output's context,
producing the on-disk target file. Drift is detected by re-rendering
and comparing to the committed output, mirroring the
``docs generate-file-conventions --check`` pattern.

Design lives in ``vibes/docs/logics/canonical-renderer.md``. This
module stays pure (no argparse, no stdout); the CLI surface is
``cmd_scaffold_canonical`` in ``application.commands``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from mozyo_bridge.shared.errors import die


CANONICAL_SOURCES_RELATIVE = Path("src/mozyo_bridge/scaffold/canonical_sources")
SCAFFOLD_OUTPUT_ROOT_RELATIVE = Path("src/mozyo_bridge/scaffold")


@dataclass(frozen=True)
class Fragment:
    id: str
    when: dict[str, str]
    body: str


@dataclass(frozen=True)
class OutputSpec:
    target: Path
    context: dict[str, str]


@dataclass(frozen=True)
class CanonicalSource:
    id: str
    source_path: Path
    outputs: tuple[OutputSpec, ...]
    fragments: tuple[Fragment, ...]


@dataclass(frozen=True)
class RenderResult:
    source_id: str
    output_path: Path
    rendered: str
    on_disk: str | None

    @property
    def drift(self) -> bool:
        return self.on_disk != self.rendered

    @property
    def reason(self) -> str:
        if self.on_disk is None:
            return "missing"
        return "out of date"


def _read_body(source_dir: Path, body_file: str) -> str:
    body_path = source_dir / body_file
    if not body_path.exists():
        die(f"canonical source body file missing: {body_path.as_posix()}")
    return body_path.read_text(encoding="utf-8")


def _normalize_context(raw: object, *, source_path: Path, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        die(f"canonical source {source_path.as_posix()} {label} must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            die(
                f"canonical source {source_path.as_posix()} {label} key must be a non-empty string"
            )
        if isinstance(value, bool) or value is None:
            die(
                f"canonical source {source_path.as_posix()} {label} value for {key!r} must be a scalar string/int"
            )
        normalized[key] = str(value)
    return normalized


def load_canonical_source(source_path: Path) -> CanonicalSource:
    if not source_path.exists():
        die(f"canonical source not found: {source_path.as_posix()}")
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        die(f"canonical source must be a YAML mapping: {source_path.as_posix()}")

    source_id = raw.get("id")
    if not isinstance(source_id, str) or not source_id:
        die(f"canonical source {source_path.as_posix()} missing `id`")

    outputs_raw = raw.get("outputs")
    if not isinstance(outputs_raw, list) or not outputs_raw:
        die(f"canonical source {source_path.as_posix()} missing `outputs`")

    fragments_raw = raw.get("fragments")
    if not isinstance(fragments_raw, list) or not fragments_raw:
        die(f"canonical source {source_path.as_posix()} missing `fragments`")

    source_dir = source_path.parent / source_id

    outputs: list[OutputSpec] = []
    for spec in outputs_raw:
        if not isinstance(spec, dict):
            die(
                f"canonical source {source_path.as_posix()} output is not a mapping: {spec!r}"
            )
        target = spec.get("target")
        if not isinstance(target, str) or not target:
            die(
                f"canonical source {source_path.as_posix()} output missing `target`: {spec!r}"
            )
        target_path = Path(target)
        if target_path.is_absolute() or ".." in target_path.parts:
            die(
                f"canonical source {source_path.as_posix()} output target must be a "
                f"repo-relative path under the scaffold tree: {target!r}"
            )
        context = _normalize_context(
            spec.get("context"), source_path=source_path, label="output context"
        )
        outputs.append(OutputSpec(target=target_path, context=context))

    fragments: list[Fragment] = []
    for spec in fragments_raw:
        if not isinstance(spec, dict):
            die(
                f"canonical source {source_path.as_posix()} fragment is not a mapping: {spec!r}"
            )
        fragment_id = spec.get("id")
        if not isinstance(fragment_id, str) or not fragment_id:
            die(
                f"canonical source {source_path.as_posix()} fragment missing `id`: {spec!r}"
            )
        when = _normalize_context(
            spec.get("when"), source_path=source_path, label=f"fragment {fragment_id!r} when"
        )
        body = spec.get("body")
        body_file = spec.get("body_file")
        if body is not None and body_file is not None:
            die(
                f"canonical source {source_path.as_posix()} fragment {fragment_id!r} "
                f"declares both `body` and `body_file`; choose one"
            )
        if body is not None:
            if not isinstance(body, str):
                die(
                    f"canonical source {source_path.as_posix()} fragment {fragment_id!r} "
                    f"body must be a string"
                )
            body_text = body
        elif body_file is not None:
            if not isinstance(body_file, str) or not body_file:
                die(
                    f"canonical source {source_path.as_posix()} fragment {fragment_id!r} "
                    f"body_file must be a non-empty string"
                )
            if Path(body_file).is_absolute() or ".." in Path(body_file).parts:
                die(
                    f"canonical source {source_path.as_posix()} fragment {fragment_id!r} "
                    f"body_file must stay inside the source directory: {body_file!r}"
                )
            body_text = _read_body(source_dir, body_file)
        else:
            die(
                f"canonical source {source_path.as_posix()} fragment {fragment_id!r} "
                f"missing `body` or `body_file`"
            )
        fragments.append(Fragment(id=fragment_id, when=when, body=body_text))

    return CanonicalSource(
        id=source_id,
        source_path=source_path,
        outputs=tuple(outputs),
        fragments=tuple(fragments),
    )


def fragment_matches(fragment: Fragment, context: dict[str, str]) -> bool:
    """Return True when every ``when`` key/value matches the context.

    An empty ``when`` matches every context. A key not present in the
    context never matches a non-empty expectation, so callers do not
    have to enumerate every variable in every output.
    """
    for key, expected in fragment.when.items():
        if context.get(key) != expected:
            return False
    return True


def render_for_context(source: CanonicalSource, context: dict[str, str]) -> str:
    parts: list[str] = []
    for fragment in source.fragments:
        if fragment_matches(fragment, context):
            parts.append(fragment.body)
    return "".join(parts)


def render_output(source: CanonicalSource, output: OutputSpec) -> str:
    return render_for_context(source, output.context)


def resolve_canonical_dir(repo_root: Path) -> Path:
    return (repo_root / CANONICAL_SOURCES_RELATIVE).resolve()


def resolve_output_path(repo_root: Path, output: OutputSpec) -> Path:
    return (repo_root / SCAFFOLD_OUTPUT_ROOT_RELATIVE / output.target).resolve()


def discover_sources(canonical_dir: Path) -> list[CanonicalSource]:
    if not canonical_dir.is_dir():
        return []
    return [load_canonical_source(path) for path in sorted(canonical_dir.glob("*.yaml"))]


def collect_render_results(repo_root: Path) -> list[RenderResult]:
    canonical_dir = resolve_canonical_dir(repo_root)
    sources = discover_sources(canonical_dir)
    if not sources:
        die(
            "no canonical sources found under "
            + canonical_dir.as_posix()
            + " (expected at least one .yaml file)"
        )
    results: list[RenderResult] = []
    for source in sources:
        for output in source.outputs:
            output_path = resolve_output_path(repo_root, output)
            rendered = render_output(source, output)
            on_disk = (
                output_path.read_text(encoding="utf-8") if output_path.exists() else None
            )
            results.append(
                RenderResult(
                    source_id=source.id,
                    output_path=output_path,
                    rendered=rendered,
                    on_disk=on_disk,
                )
            )
    return results


def write_render_results(results: list[RenderResult]) -> list[Path]:
    written: list[Path] = []
    for result in results:
        result.output_path.parent.mkdir(parents=True, exist_ok=True)
        result.output_path.write_text(result.rendered, encoding="utf-8")
        written.append(result.output_path)
    return written
