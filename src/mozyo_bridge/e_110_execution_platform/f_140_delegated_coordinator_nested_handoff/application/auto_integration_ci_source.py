"""The CI's CURRENT verdict for a commit, read at action time (review j#96650 finding 5).

The durable ``required_ci_green`` marker is an attestation: the coordinator recorded that a
required check concluded successfully for a head. What it structurally cannot say is that the
head later went red — the canonical producer renders ``conclusion=success`` and nothing else, so
a failure leaves no marker to supersede the green one. R1 and R2 both documented that boundary
and left it as a ruling to be sought; the finding is that documenting a fail-OPEN gate does not
close it, and item 1 asks for the CI ``conclusion`` to be read at action time.

**This closes it without touching the marker vocabulary.** Extending
``### Hibernate Evidence Marker Contract`` to carry a failure conclusion would be a change to a
central-preset-owned contract — a guardrail surface this role does not edit on its own authority.
The correction condition allows "durable grammar **or source**", so this is the source: the CI
provider itself, asked about the exact commit, at the moment the gate is evaluated.

The two are a conjunction, and each covers what the other cannot:

- the **marker** says a required check was attested green by the coordinator — an authority
  decision, durable and auditable, which a live API cannot supply;
- this **source** says the provider's current terminal verdict for that commit, workflow, and
  branch context is not a failure — a fact that changes after the attestation is written, which
  a durable record cannot supply.

Both must hold. A commit whose attested-green run was superseded by a red one now fails the gate
before any mutation, which is the behaviour the finding asks for.

**Unavailable is not success.** A provider that cannot be reached, a CLI that is not installed,
output that cannot be parsed — every one of them yields :data:`CI_STATE_UNAVAILABLE`, and the
consumer treats that as "no current CI authority", which closes the gate. That makes the gate
require a working CI source, and it should: the preset says in as many words that the CI gate is
not a configurable one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    is_full_sha,
)

#: The latest matching run for this commit and required workflow concluded successfully.
CI_STATE_SUCCESS = "success"
#: The latest matching run concluded as something other than success. This is the state the
#: durable marker cannot express, and the reason this source exists.
CI_STATE_FAILURE = "failure"
#: Runs exist for this commit and none has reached a terminal conclusion yet.
CI_STATE_PENDING = "pending"
#: The provider could not be asked, or its answer could not be read. NOT a success.
CI_STATE_UNAVAILABLE = "unavailable"

CI_STATES = frozenset(
    {CI_STATE_SUCCESS, CI_STATE_FAILURE, CI_STATE_PENDING, CI_STATE_UNAVAILABLE}
)

#: A bounded read: the gate is evaluated inside an interactive action, so an unreachable
#: provider must fail rather than hang.
DEFAULT_CI_QUERY_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class CiVerdict:
    """The provider's current answer about one commit/workflow/branch context."""

    state: str
    detail: str = ""
    run: str = ""
    workflow: str = ""
    commit: str = ""
    branch: str = ""
    conclusion: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this answer must stop a mutation.

        Everything except an outright ``success`` does. ``pending`` blocks because the run has
        not concluded; ``unavailable`` blocks because we cannot tell — and a gate that opens
        when it cannot see is not a gate.
        """
        return self.state != CI_STATE_SUCCESS


@runtime_checkable
class CiStatusReader(Protocol):
    """Reads the CI provider's CURRENT verdict for an exact action context."""

    def verdict_for(
        self,
        commit: str,
        *,
        workflow: str = "",
        attested_run: str = "",
        branch: str = "",
    ) -> CiVerdict: ...


@dataclass(frozen=True)
class GhCliCiStatusReader:
    """A :class:`CiStatusReader` over GitHub's actions-runs API.

    ``gh api --method GET .../actions/runs -f head_sha=<sha>`` is a read-only query supported by
    the installed GitHub CLI as well as current releases. The answer is bound to both commit and
    branch context: an issue-branch quick run for a fast-forwarded SHA is not the integration
    batch the same SHA must run after landing on ``main``.
    """

    repo_root: Path
    timeout: float = DEFAULT_CI_QUERY_TIMEOUT_SECONDS
    executable: str = "gh"

    def verdict_for(
        self,
        commit: str,
        *,
        workflow: str = "",
        attested_run: str = "",
        branch: str = "",
    ) -> CiVerdict:
        if not is_full_sha(commit):
            return CiVerdict(
                CI_STATE_UNAVAILABLE, "a CI verdict is asked about a full commit SHA"
            )
        if shutil.which(self.executable) is None:
            return CiVerdict(
                CI_STATE_UNAVAILABLE,
                f"{self.executable!r} is not on PATH, so the provider cannot be asked",
            )
        try:
            proc = subprocess.run(
                [
                    self.executable,
                    "api",
                    "repos/{owner}/{repo}/actions/runs",
                    "--method",
                    "GET",
                    "-f",
                    f"head_sha={commit}",
                    "-f",
                    "per_page=100",
                ],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CiVerdict(
                CI_STATE_UNAVAILABLE,
                f"the provider query failed ({exc.__class__.__name__})",
            )
        if proc.returncode != 0:
            return CiVerdict(
                CI_STATE_UNAVAILABLE,
                f"the provider query exited {proc.returncode}",
            )
        try:
            response = json.loads(proc.stdout or "{}")
        except ValueError:
            return CiVerdict(CI_STATE_UNAVAILABLE, "the provider's answer was unreadable")
        if not isinstance(response, dict):
            return CiVerdict(CI_STATE_UNAVAILABLE, "the provider's answer was not an object")
        raw_runs = response.get("workflow_runs")
        if not isinstance(raw_runs, Sequence) or isinstance(raw_runs, str):
            return CiVerdict(CI_STATE_UNAVAILABLE, "the provider's answer had no run list")
        runs = []
        for raw in raw_runs:
            if not isinstance(raw, dict):
                return CiVerdict(CI_STATE_UNAVAILABLE, "a provider run was not an object")
            runs.append(
                {
                    "status": raw.get("status"),
                    "conclusion": raw.get("conclusion"),
                    "workflowName": raw.get("name"),
                    "databaseId": raw.get("id"),
                    "createdAt": raw.get("created_at"),
                    "headSha": raw.get("head_sha"),
                    "headBranch": raw.get("head_branch"),
                    "event": raw.get("event"),
                }
            )
        return classify_runs(
            runs,
            workflow=str(workflow or ""),
            attested_run=str(attested_run or ""),
            commit=commit,
            branch=str(branch or ""),
        )


def classify_runs(
    runs: Sequence[object],
    *,
    workflow: str = "",
    attested_run: str = "",
    commit: str = "",
    branch: str = "",
) -> CiVerdict:
    """Fold a provider run list into one verdict (pure, so it is testable without a provider).

    The durable marker names the required workflow and the run it attested.  Runs from another
    workflow are not evidence about that gate. Within the required workflow and branch, the
    latest run for the exact commit is authoritative: a newer failure or pending rerun withdraws
    an older green, while a newer successful rerun is current provider truth. GitHub is
    newest-first; ``createdAt`` / ``databaseId`` make that order explicit when present, and the
    provider order is the bounded fallback when they are absent.
    """
    if not runs:
        return CiVerdict(
            CI_STATE_UNAVAILABLE, "the provider reports no runs for this commit"
        )
    selected = []
    wanted_workflow = str(workflow or "").strip()
    wanted_commit = str(commit or "").strip()
    wanted_branch = str(branch or "").strip()
    if wanted_commit and not is_full_sha(wanted_commit):
        return CiVerdict(CI_STATE_UNAVAILABLE, "the exact CI commit was malformed")
    for position, run in enumerate(runs):
        if not isinstance(run, dict):
            return CiVerdict(CI_STATE_UNAVAILABLE, "a run entry was not an object")
        if wanted_workflow and str(run.get("workflowName", "") or "").strip() != wanted_workflow:
            continue
        # The API's ``head_sha`` query is expected to enforce this server-side. Check the returned
        # identity too: a response that omits or drifts the head is not evidence about the commit
        # the gate asked for.
        if wanted_commit and str(run.get("headSha", "") or "").strip() != wanted_commit:
            continue
        if wanted_branch and str(run.get("headBranch", "") or "").strip() != wanted_branch:
            continue
        selected.append((position, run))
    if not selected:
        scope = "/".join(part for part in (wanted_workflow, wanted_branch) if part)
        suffix = f" for {scope!r}" if scope else ""
        return CiVerdict(CI_STATE_UNAVAILABLE, f"the provider reports no matching runs{suffix}")

    wanted_run = str(attested_run or "").strip()
    if wanted_run:
        attested = [
            run
            for _, run in selected
            if str(run.get("databaseId", "") or "").strip() == wanted_run
        ]
        if len(attested) != 1:
            return CiVerdict(
                CI_STATE_UNAVAILABLE,
                f"the attested run {wanted_run!r} was not unique in the provider's bounded answer",
            )
        attested_conclusion = str(attested[0].get("conclusion", "") or "").strip().lower()
        if not attested_conclusion:
            return CiVerdict(
                CI_STATE_PENDING,
                f"the attested run {wanted_run!r} no longer has a terminal conclusion",
                run=wanted_run,
                workflow=wanted_workflow,
                commit=wanted_commit,
                branch=wanted_branch,
            )
        if attested_conclusion != CI_STATE_SUCCESS:
            return CiVerdict(
                CI_STATE_FAILURE,
                f"the attested run {wanted_run!r} currently concludes {attested_conclusion!r}",
                run=wanted_run,
                workflow=wanted_workflow,
                commit=wanted_commit,
                branch=wanted_branch,
                conclusion=attested_conclusion,
            )

    def order_key(item):
        position, run = item
        created = str(run.get("createdAt", "") or "").strip()
        try:
            database_id = int(run.get("databaseId", -1))
        except (TypeError, ValueError):
            database_id = -1
        # With no provider ordering fields, position 0 is newest (the gh contract).
        return (bool(created), created, database_id, -position)

    _, latest = max(selected, key=order_key)
    conclusion = str(latest.get("conclusion", "") or "").strip().lower()
    run_id = latest.get("databaseId", "?")
    run_workflow = latest.get("workflowName", "?")
    run_branch = str(latest.get("headBranch", "") or "").strip()
    run_commit = str(latest.get("headSha", "") or "").strip()
    if not conclusion:
        return CiVerdict(
            CI_STATE_PENDING,
            f"latest run {run_id} ({run_workflow}) has not concluded",
            run=str(run_id),
            workflow=str(run_workflow),
            commit=run_commit,
            branch=run_branch,
        )
    if conclusion == CI_STATE_SUCCESS:
        return CiVerdict(
            CI_STATE_SUCCESS,
            f"latest run {run_id} ({run_workflow}) concluded success",
            run=str(run_id),
            workflow=str(run_workflow),
            commit=run_commit,
            branch=run_branch,
            conclusion=conclusion,
        )
    return CiVerdict(
        CI_STATE_FAILURE,
        f"latest run {run_id} ({run_workflow}) concluded {conclusion!r}",
        run=str(run_id),
        workflow=str(run_workflow),
        commit=run_commit,
        branch=run_branch,
        conclusion=conclusion,
    )


__all__ = (
    "CI_STATES",
    "CI_STATE_FAILURE",
    "CI_STATE_PENDING",
    "CI_STATE_SUCCESS",
    "CI_STATE_UNAVAILABLE",
    "DEFAULT_CI_QUERY_TIMEOUT_SECONDS",
    "CiStatusReader",
    "CiVerdict",
    "GhCliCiStatusReader",
    "classify_runs",
)
