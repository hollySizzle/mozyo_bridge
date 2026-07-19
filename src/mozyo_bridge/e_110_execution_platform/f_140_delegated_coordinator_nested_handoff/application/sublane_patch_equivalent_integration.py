"""Action-time resolver for a ``patch_equivalent`` integration disposition (Redmine #14066).

The application half of the #14066 fence. The AUTHORITY is the exact Redmine integration
journal, read fresh at action-time through the credential-gated
:class:`...live_redmine_journal_source.LiveRedmineJournalSource` (Redmine #14066 review j#82298
F1): a caller-supplied local file is never trusted. The coordinator embeds the structured
``patch_equivalent`` disposition as a fenced ``mozyo-patch-equivalent-integration`` block in the
durable integration journal; this resolver fetches the issue's journals, locates the EXACT
journal by its own Redmine record id (never a self-reported field — the dispatch-marker
authority principle), parses the disposition block, **recomputes** the real git facts (current
branch / integration heads, unintegrated-by-hash set, per-commit stable patch-ids,
integration-commit reachability, and origin reachability against a FRESH read-only remote
observation of the canonical ``origin`` integration branch — ``git ls-remote``, not a cached
tracking ref a caller could forge), and consults the pure fence.

The #13845 bound terminal retire consults the returned :class:`PatchEquivalentResolution` ONLY
when the literal-ancestor probe already reported the head un-integrated, so the literal path
never even constructs this resolver (Redmine #14066 review j#82298 F2 — the guard lives in the
command handler).

Fail-closed everywhere: an unsupplied integration journal returns ``None`` (the retire keeps its
literal ``head_not_integrated``); unconfigured / missing credentials, an unreadable Redmine, a
journal not found, an absent / ambiguous / malformed disposition block, an unresolvable git ref,
or any fence refusal returns a non-admissible resolution with a closed-vocabulary reason.
Nothing here writes anything; every git call is read-only and the Redmine fetch is a read-only,
redirect-refusing GET.

Boundary (Redmine #14066): read-only git probes (``rev-parse`` / ``rev-list`` / ``merge-base
--is-ancestor`` / ``show`` + ``patch-id`` / ``ls-remote``) + one credential-gated read-only
Redmine journal fetch. ``ls-remote`` is a read-only remote query (no object download, no ref
mutation); there is no ``git fetch``/push/ref mutation, no Redmine write, no process / worktree /
branch actuation.
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
    LiveRedmineJournalError,
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.patch_equivalent_integration import (  # noqa: E501
    AdmissionResult,
    PatchEquivalentDisposition,
    PatchEquivalentObservation,
    disposition_from_block,
    evaluate_patch_equivalent_integrated,
    parse_integration_disposition_blocks,
)

#: The canonical remote whose ``<remote>/<integration_branch>`` ref the integration head's origin
#: reachability is bound to. The disposition never names the ref (a caller could otherwise point
#: it at a local branch), so the retire derives it — review j#82298 F1.
CANONICAL_REMOTE = "origin"

#: Application-layer fail-closed reasons (distinct from the pure fence's evidence-vs-facts
#: reasons): the durable authority could not be reached / located, or the git probe could not
#: resolve the branch identities it needs to measure equivalence at all.
PE_REDMINE_UNCONFIGURED = "redmine_credentials_unconfigured"
PE_REDMINE_UNREADABLE = "redmine_journal_unreadable"
PE_JOURNAL_NOT_FOUND = "integration_journal_not_found"
PE_DISPOSITION_ABSENT = "integration_disposition_absent"
PE_DISPOSITION_AMBIGUOUS = "integration_disposition_ambiguous"
PE_DISPOSITION_MALFORMED = "integration_disposition_malformed"
PE_PROBE_UNRESOLVED = "integration_probe_unresolved"


@dataclass(frozen=True)
class PatchEquivalentResolution:
    """The fail-closed action-time verdict for a supplied integration journal.

    ``admissible`` is true only when the exact Redmine journal was read, its disposition block
    parsed, the git facts resolved, and the pure fence proved every axis. Every other outcome is
    ``admissible=False`` with a closed-vocabulary ``reason`` (the pure fence's, or an
    application-layer one) and a ``detail``.
    """

    admissible: bool
    reason: str
    detail: str = ""

    def as_payload(self) -> dict:
        return {"admissible": self.admissible, "reason": self.reason, "detail": self.detail}


def _fail(reason: str, detail: str) -> PatchEquivalentResolution:
    return PatchEquivalentResolution(admissible=False, reason=reason, detail=detail)


def read_integration_disposition(
    issue: str, integration_journal: str
) -> tuple[Optional[PatchEquivalentDisposition], Optional[PatchEquivalentResolution]]:
    """Fresh-read the EXACT Redmine integration journal and parse its disposition block.

    Returns ``(disposition, None)`` on success, or ``(None, resolution)`` with a fail-closed
    resolution. The Redmine read is credential-gated via
    :meth:`LiveRedmineJournalSource.from_environment` (env / home-scoped credentials only — a
    repo-local file can never supply the key), so missing credentials fail closed
    (:data:`PE_REDMINE_UNCONFIGURED`) and an unreadable Redmine fails closed
    (:data:`PE_REDMINE_UNREADABLE`). The journal is located by its OWN Redmine record id
    (``entry.journal_id``), never a self-reported field. A journal absent
    (:data:`PE_JOURNAL_NOT_FOUND`), carrying no disposition block
    (:data:`PE_DISPOSITION_ABSENT`), carrying more than one (:data:`PE_DISPOSITION_AMBIGUOUS`),
    or one that will not project (:data:`PE_DISPOSITION_MALFORMED`) each fails closed.
    """
    try:
        source = LiveRedmineJournalSource.from_environment()
    except LiveRedmineJournalError as exc:
        return None, _fail(
            PE_REDMINE_UNCONFIGURED,
            f"the Redmine credentials are unconfigured ({exc}); the exact integration journal "
            "cannot be re-read, so the terminal retire fails closed",
        )
    try:
        entries = source.read_entries(issue)
    except LiveRedmineJournalError as exc:
        return None, _fail(
            PE_REDMINE_UNREADABLE,
            f"the Redmine integration journal could not be read ({exc}); the terminal retire "
            "fails closed",
        )
    target = integration_journal.strip()
    matched = [
        entry
        for entry in entries
        if str(getattr(entry, "journal_id", "") or "").strip() == target
    ]
    if not matched:
        return None, _fail(
            PE_JOURNAL_NOT_FOUND,
            f"issue #{issue} carries no journal {target!r}; the named integration disposition "
            "journal does not exist, so the terminal retire fails closed",
        )
    # RAW fence occurrences, counted before any JSON validation (review j#82301 F2): a malformed
    # block still counts, so a malformed-plus-valid pair is ambiguous, never silently collapsed to
    # the one valid block.
    raw_blocks: list[str] = []
    for entry in matched:
        raw_blocks.extend(
            parse_integration_disposition_blocks(getattr(entry, "notes", "") or "")
        )
    if not raw_blocks:
        return None, _fail(
            PE_DISPOSITION_ABSENT,
            f"journal {target!r} carries no `mozyo-patch-equivalent-integration` disposition "
            "block; there is no structured integration disposition to verify",
        )
    if len(raw_blocks) > 1:
        return None, _fail(
            PE_DISPOSITION_AMBIGUOUS,
            f"journal {target!r} carries {len(raw_blocks)} disposition blocks; an ambiguous "
            "durable record cannot license a terminal retire",
        )
    disposition = disposition_from_block(raw_blocks[0])
    if disposition is None:
        return None, _fail(
            PE_DISPOSITION_MALFORMED,
            f"the disposition block in journal {target!r} is malformed (not a JSON object, a "
            "missing / non-list commit_map, a non-string field, or a non-boolean "
            "origin_reachable); it cannot be verified",
        )
    return disposition, None


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
    result = _run_git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant)
    return result is not None and result.returncode == 0


def _rev_list(repo_root: Path, base: str, tip: str) -> Optional[frozenset[str]]:
    result = _run_git(repo_root, "rev-list", f"{base}..{tip}")
    if result is None or result.returncode != 0:
        return None
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def _remote_head(repo_root: Path, remote: str, branch: str) -> Optional[str]:
    """The CURRENT head SHA of ``<remote>/refs/heads/<branch>`` via a read-only remote query.

    ``git ls-remote`` performs a read-only remote observation (no fetch, no object download, no
    ref mutation) so the answer reflects the actual origin RIGHT NOW — not a cached
    ``refs/remotes/origin/*`` tracking ref that a stale checkout or a local ``update-ref`` could
    forge (Redmine #14066 review j#82301 F1). ``None`` when the remote is unreadable or the branch
    is absent on the remote, so a missing / dropped integration branch fails closed.
    """
    if not remote or not branch:
        return None
    result = _run_git(repo_root, "ls-remote", "--heads", remote, branch)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
            sha = parts[0].strip()
            if sha:
                return sha
    return None


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
    repo_root: Path,
    disposition: PatchEquivalentDisposition,
    *,
    branch: str,
    integration_branch: str,
) -> Optional[PatchEquivalentObservation]:
    """Recompute the real git facts for the fence. ``None`` when the branch identities won't resolve.

    Origin reachability is measured against a FRESH read-only remote observation of the canonical
    ``origin`` integration branch (``git ls-remote``), never a cached ``origin/<branch>``
    tracking ref a caller could forge (review j#82301 F1): the integration head must be an ancestor
    of the origin branch's CURRENT head. All reads are read-only (``ls-remote`` does not fetch).
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
            reachable[integ] = _is_ancestor(repo_root, integ, actual_integration_head)
    # The canonical origin branch's CURRENT head, observed action-time (not a cached tracking ref).
    # The integration head is origin-reachable only if it is an ancestor of that live remote head;
    # a missing remote branch (``None``) or an unresolvable / non-local remote head fails closed.
    remote_head = _remote_head(repo_root, CANONICAL_REMOTE, integration_branch)
    origin_reachable = bool(remote_head) and _is_ancestor(
        repo_root, actual_integration_head, remote_head
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
    """Resolve the named integration journal action-time. ``None`` when unsupplied.

    Reads ``--integration-journal``; ``None`` (unsupplied) leaves the retire on its literal
    -ancestor path. Otherwise it fresh-reads the EXACT Redmine journal as authority
    (credential-gated), recomputes the git facts, and consults the pure fence. Every read /
    probe / fence failure returns a non-admissible :class:`PatchEquivalentResolution`.
    """
    integration_journal = (getattr(args, "integration_journal", None) or "").strip()
    if not integration_journal:
        return None
    issue = (getattr(args, "issue", "") or "").strip()
    if not issue:
        return _fail(
            PE_JOURNAL_NOT_FOUND,
            "no --issue to locate the integration journal against; fail closed",
        )
    disposition, failure = read_integration_disposition(issue, integration_journal)
    if failure is not None:
        return failure
    branch = (getattr(args, "branch", "") or "").strip()
    integration_branch = (getattr(args, "integration_branch", "") or "").strip()
    observation = probe_patch_equivalent_observation(
        repo_root, disposition, branch=branch, integration_branch=integration_branch
    )
    if observation is None:
        return _fail(
            PE_PROBE_UNRESOLVED,
            "the git probe could not resolve --branch / --integration-branch heads or the "
            "unintegrated commit set; patch-equivalence cannot be measured, so the terminal "
            "retire fails closed",
        )
    verdict: AdmissionResult = evaluate_patch_equivalent_integrated(
        disposition,
        observation,
        issue=issue,
        lane=(getattr(args, "lane_label", "") or "").strip(),
        branch=branch,
        integration_branch=integration_branch,
    )
    return PatchEquivalentResolution(
        admissible=verdict.admissible, reason=verdict.reason, detail=verdict.detail
    )


__all__ = (
    "CANONICAL_REMOTE",
    "PE_REDMINE_UNCONFIGURED",
    "PE_REDMINE_UNREADABLE",
    "PE_JOURNAL_NOT_FOUND",
    "PE_DISPOSITION_ABSENT",
    "PE_DISPOSITION_AMBIGUOUS",
    "PE_DISPOSITION_MALFORMED",
    "PE_PROBE_UNRESOLVED",
    "PatchEquivalentResolution",
    "read_integration_disposition",
    "probe_patch_equivalent_observation",
    "resolve_patch_equivalent_integration",
)
