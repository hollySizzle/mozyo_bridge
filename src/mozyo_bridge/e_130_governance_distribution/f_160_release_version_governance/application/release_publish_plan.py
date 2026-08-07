"""Exact Git authority and command rendering for ``release publish --plan``."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class ReleasePlanAuthorityError(ValueError):
    """The current checkout cannot name an exact releasable branch authority."""


@dataclass(frozen=True)
class ReleasePlanAuthority:
    source_sha: str
    source_ref: str

    @property
    def branch(self) -> str:
        return self.source_ref.removeprefix("refs/heads/")


def resolve_release_plan_authority(
    repo_root: Path, *, run: RunCommand
) -> ReleasePlanAuthority:
    """Resolve full HEAD plus its attached ``refs/heads/*`` name, fail-closed."""
    head = run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_root)
    if head.returncode != 0:
        detail = head.stderr.strip() or head.stdout.strip() or "git rev-parse failed"
        raise ReleasePlanAuthorityError(
            f"release publish --plan cannot resolve HEAD: {detail}"
        )

    symbolic = run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=repo_root)
    if symbolic.returncode == 1:
        raise ReleasePlanAuthorityError(
            "release publish --plan refuses detached HEAD because it cannot "
            "produce the required --source-ref authority"
        )
    if symbolic.returncode != 0:
        detail = (
            symbolic.stderr.strip()
            or symbolic.stdout.strip()
            or "git symbolic-ref failed"
        )
        raise ReleasePlanAuthorityError(
            f"release publish --plan cannot resolve the current branch: {detail}"
        )
    source_ref = symbolic.stdout.strip()
    if not source_ref.startswith("refs/heads/"):
        raise ReleasePlanAuthorityError(
            "release publish --plan requires a local branch under refs/heads/, "
            f"got {source_ref!r}"
        )
    return ReleasePlanAuthority(
        source_sha=head.stdout.strip(), source_ref=source_ref
    )


def render_testpypi_command(
    authority: ReleasePlanAuthority, *, version: str
) -> str:
    """Render the complete current TestPyPI CLI, shell-parseably."""
    argv: Sequence[str] = (
        "mozyo-bridge",
        "release",
        "publish",
        "--testpypi",
        "--source-sha",
        authority.source_sha,
        "--expected-version",
        version,
        "--source-ref",
        authority.source_ref,
    )
    return shlex.join(argv)


__all__ = (
    "ReleasePlanAuthority",
    "ReleasePlanAuthorityError",
    "render_testpypi_command",
    "resolve_release_plan_authority",
)
