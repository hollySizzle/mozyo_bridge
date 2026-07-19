"""Action-time resolver for a ``patch_equivalent`` integration disposition (Redmine #14066).

The application half of the #14066 fence: it reads the coordinator's durable integration
disposition (the exact Redmine integration journal's structured ``patch_equivalent`` block,
captured to a JSON observation — the same "coordinator-produced durable observation, MEASURED at
action-time" shape the #13518 ``--review-generation-json`` review fence already uses),
**recomputes** the real git facts (current branch / integration heads, the unintegrated-by-hash
commit set, per-commit stable patch-ids, integration-commit reachability, origin reachability),
and consults the pure :func:`...patch_equivalent_integration.evaluate_patch_equivalent_integrated`
fence. It returns a fail-closed :class:`PatchEquivalentResolution` the #13845 bound terminal
retire consults ONLY when the literal-ancestor probe already reported the head un-integrated —
so the literal path stays byte-identical and this never widens it, only adds the cherry-picked
alternative.

Fail-closed everywhere: an unsupplied disposition returns ``None`` (the retire keeps its literal
``head_not_integrated``); an unreadable / malformed disposition, an unresolvable git ref, or any
disagreement between the claim and the recomputed facts returns a non-admissible resolution with
a closed-vocabulary reason. Nothing here writes anything; every git call is read-only.

Boundary (Redmine #14066): read-only git probes only (``rev-parse`` / ``rev-list`` /
``merge-base --is-ancestor`` / ``show`` + ``patch-id``). No fetch, no push, no ref mutation, no
process / worktree / branch actuation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.patch_equivalent_integration import (  # noqa: E501
    AdmissionResult,
    CommitPatchMapping,
    PatchEquivalentDisposition,
    PatchEquivalentObservation,
    evaluate_patch_equivalent_integrated,
)

#: Application-layer fail-closed reasons (distinct from the pure fence's evidence-vs-facts
#: reasons): the durable disposition itself could not be read, or the git probe could not
#: resolve the branch identities it needs to measure equivalence at all.
PE_EVIDENCE_UNREADABLE = "integration_disposition_unreadable"
PE_PROBE_UNRESOLVED = "integration_probe_unresolved"


@dataclass(frozen=True)
class PatchEquivalentResolution:
    """The fail-closed action-time verdict for a supplied patch-equivalent disposition.

    ``admissible`` is true only when the disposition was readable, the git facts resolved, and
    the pure fence proved every axis. Every other outcome is ``admissible=False`` with a
    closed-vocabulary ``reason`` (the pure fence's, or an application-layer one) and a ``detail``.
    """

    admissible: bool
    reason: str
    detail: str = ""

    def as_payload(self) -> dict:
        return {"admissible": self.admissible, "reason": self.reason, "detail": self.detail}


def load_patch_equivalent_disposition(
    path: str,
) -> Optional[PatchEquivalentDisposition]:
    """Read the coordinator's durable integration disposition JSON. ``None`` on any read error.

    The JSON mirrors the exact Redmine integration journal's structured ``patch_equivalent``
    block::

        {
          "issue": "13846", "lane": "issue_13846_...", "branch": "issue_13846_...",
          "integration_branch": "int_13472_session_continuity",
          "source_head": "<40-hex>", "integration_head": "<40-hex>",
          "origin_ref": "origin/int_13472_session_continuity", "origin_reachable": true,
          "journal_id": "82xxx",
          "commit_map": [{"source": "<40-hex>", "integration": "<40-hex>", "patch_id": "<hex>"}]
        }

    A missing file, malformed JSON, a non-object root, or a malformed ``commit_map`` entry
    returns ``None`` (the caller fails closed with :data:`PE_EVIDENCE_UNREADABLE`). Commit hashes
    are compared verbatim downstream, so they must be canonical full hashes.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    entries = raw.get("commit_map")
    if not isinstance(entries, list):
        return None
    mappings: list[CommitPatchMapping] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        mappings.append(
            CommitPatchMapping(
                source_commit=str(entry.get("source", "")),
                integration_commit=str(entry.get("integration", "")),
                patch_id=str(entry.get("patch_id", "")),
            )
        )
    return PatchEquivalentDisposition(
        issue=str(raw.get("issue", "")),
        lane=str(raw.get("lane", "")),
        branch=str(raw.get("branch", "")),
        integration_branch=str(raw.get("integration_branch", "")),
        source_head=str(raw.get("source_head", "")),
        integration_head=str(raw.get("integration_head", "")),
        origin_ref=str(raw.get("origin_ref", "")),
        origin_reachable=bool(raw.get("origin_reachable", False)),
        commit_map=tuple(mappings),
        journal_id=str(raw.get("journal_id", "")),
    )


def _run_git(repo_root: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            text=True,
            capture_output=True,
        )
    except OSError:
        return None


def _rev_parse(repo_root: Path, ref: str) -> Optional[str]:
    if not ref:
        return None
    result = _run_git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result is None or result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    result = _run_git(
        repo_root, "merge-base", "--is-ancestor", ancestor, descendant
    )
    return result is not None and result.returncode == 0


def _rev_list(repo_root: Path, base: str, tip: str) -> Optional[frozenset[str]]:
    result = _run_git(repo_root, "rev-list", f"{base}..{tip}")
    if result is None or result.returncode != 0:
        return None
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def _stable_patch_id(repo_root: Path, sha: str) -> str:
    """Recompute ``git patch-id --stable`` for ``sha`` (empty string when unresolvable).

    ``git show --format=`` emits just the commit's diff (no header); ``git patch-id --stable``
    folds it to a hash-order-independent id. The second ``patch-id`` column (a commit id derived
    from the diff) is ignored — only the patch-id (first column) is used, so a cherry-pick with a
    different commit hash but an identical diff yields the SAME id, which is the whole point.
    """
    if not sha:
        return ""
    show = _run_git(repo_root, "show", "--no-color", "--format=", sha)
    if show is None or show.returncode != 0 or not show.stdout:
        return ""
    try:
        patch = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=show.stdout,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    if patch.returncode != 0:
        return ""
    out = patch.stdout.strip()
    if not out:
        return ""
    return out.split()[0]


def probe_patch_equivalent_observation(
    repo_root: Path, disposition: PatchEquivalentDisposition, *, branch: str, integration_branch: str
) -> Optional[PatchEquivalentObservation]:
    """Recompute the real git facts for the fence. ``None`` when the branch identities won't resolve.

    Reads the current heads, the unintegrated-by-hash commit set, per-commit stable patch-ids for
    every mapped commit, each mapped integration commit's reachability from the integration head,
    and the integration head's reachability from the disposition's origin ref. All read-only.
    """
    actual_source_head = _rev_parse(repo_root, branch)
    actual_integration_head = _rev_parse(repo_root, integration_branch)
    if actual_source_head is None or actual_integration_head is None:
        return None
    unintegrated = _rev_list(repo_root, integration_branch, branch)
    if unintegrated is None:
        return None
    patch_ids: dict[str, str] = {}
    reachable: dict[str, bool] = {}
    for commit in unintegrated:
        patch_ids[commit] = _stable_patch_id(repo_root, commit)
    for mapping in disposition.commit_map:
        src = mapping.source_commit.strip()
        integ = mapping.integration_commit.strip()
        if src and src not in patch_ids:
            patch_ids[src] = _stable_patch_id(repo_root, src)
        if integ and integ not in patch_ids:
            patch_ids[integ] = _stable_patch_id(repo_root, integ)
        if integ:
            reachable[integ] = _is_ancestor(
                repo_root, integ, actual_integration_head
            )
    origin_reachable = _is_ancestor(
        repo_root, actual_integration_head, disposition.origin_ref
    )
    return PatchEquivalentObservation(
        actual_source_head=actual_source_head,
        actual_integration_head=actual_integration_head,
        unintegrated_source_commits=unintegrated,
        integration_commit_reachable=reachable,
        patch_ids=patch_ids,
        integration_head_origin_reachable=origin_reachable,
    )


def resolve_patch_equivalent_integration(
    args: argparse.Namespace, repo_root: Path
) -> Optional[PatchEquivalentResolution]:
    """Resolve the supplied patch-equivalent disposition action-time. ``None`` when unsupplied.

    Reads ``--integration-disposition-json``; ``None`` (unsupplied) leaves the retire on its
    literal-ancestor path. An unreadable / malformed disposition, an unresolvable git identity,
    or a fence refusal returns a non-admissible :class:`PatchEquivalentResolution` (fail-closed).
    """
    path = (getattr(args, "integration_disposition_json", None) or "").strip()
    if not path:
        return None
    disposition = load_patch_equivalent_disposition(path)
    if disposition is None:
        return PatchEquivalentResolution(
            admissible=False,
            reason=PE_EVIDENCE_UNREADABLE,
            detail=(
                "the --integration-disposition-json evidence is missing / unreadable / "
                "malformed; the coordinator's durable patch-equivalent integration disposition "
                "cannot be re-read, so the terminal retire fails closed"
            ),
        )
    branch = (getattr(args, "branch", "") or "").strip()
    integration_branch = (getattr(args, "integration_branch", "") or "").strip()
    observation = probe_patch_equivalent_observation(
        repo_root, disposition, branch=branch, integration_branch=integration_branch
    )
    if observation is None:
        return PatchEquivalentResolution(
            admissible=False,
            reason=PE_PROBE_UNRESOLVED,
            detail=(
                "the git probe could not resolve --branch / --integration-branch heads or the "
                "unintegrated commit set; patch-equivalence cannot be measured, so the terminal "
                "retire fails closed"
            ),
        )
    verdict: AdmissionResult = evaluate_patch_equivalent_integrated(
        disposition,
        observation,
        issue=(getattr(args, "issue", "") or "").strip(),
        lane=(getattr(args, "lane_label", "") or "").strip(),
        branch=branch,
        integration_branch=integration_branch,
    )
    return PatchEquivalentResolution(
        admissible=verdict.admissible, reason=verdict.reason, detail=verdict.detail
    )


__all__ = (
    "PE_EVIDENCE_UNREADABLE",
    "PE_PROBE_UNRESOLVED",
    "PatchEquivalentResolution",
    "load_patch_equivalent_disposition",
    "probe_patch_equivalent_observation",
    "resolve_patch_equivalent_integration",
)
