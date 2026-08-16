"""`scaffold apply` target advice — say when the target will not be the workspace.

Redmine #15526. Measured: `scaffold apply --target /myapp/Source/rails` inside a Git
repository rooted at `/myapp` writes every file and exits 0 without a word, and the
`mozyo` run from that same directory then resolves to `/myapp`, finds no marker, and
tells the operator to scaffold the project — the thing they just did. The scaffold is
real, it is simply in a directory the resolver walks past, because Git-root-first
identity (#13641) is what keeps a subtree from shadowing the root's declared terminal
backend.

The rule stays. This module supplies the sentence the rule was missing, BEFORE the
write, so the operator can retarget instead of discovering it at launch time.

It is a **warning, not a refusal**. Scaffolding a project subdirectory on purpose is
legitimate — a monorepo project that is not itself a Git root still wants its routers
and manifests — and refusing would break that. What is not legitimate is doing it by
accident and being told nothing.

Pure: it decides from two paths and performs no I/O, so the exact wording is testable
without a filesystem.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ("nested_target_warning",)


def nested_target_warning(target: Path, git_root: Path | None) -> str | None:
    """Advice for scaffolding ``target`` when ``git_root`` sits above it, else ``None``.

    ``git_root`` is :func:`mozyo_bridge.shared.paths.infer_git_worktree_root` of the
    target. The Git root is deliberately the only trigger: it is the exact condition
    under which :func:`~mozyo_bridge.shared.paths.find_repo_root` walks past a marker.
    Widening this to the marker walk would fire on a genuinely non-git scaffolded
    workspace (#11301) or a registry-anchored one (#11429), where the target really is
    the root and there is nothing to warn about.
    """
    if git_root is None or git_root == target:
        return None
    return (
        f"note: {target} is not the root that mozyo will resolve. A Git worktree "
        f"root exists above it at {git_root}, and the Git root is the workspace "
        f"identity (Redmine #13641), so a marker written here is walked past rather "
        f"than adopted — `mozyo` run from this directory will resolve {git_root} and "
        f"refuse if THAT root is unadopted. Scaffolding a project subdirectory on "
        f"purpose is fine and this is only a note. If you meant to adopt the "
        f"workspace, re-run with `--target {git_root}`. If you meant this subtree to "
        f"be its own workspace, point the CLI at it explicitly (`--repo {target}` or "
        f"MOZYO_REPO) and declare its relationship to the parent with "
        f"`mozyo-bridge workspace alias`."
    )
