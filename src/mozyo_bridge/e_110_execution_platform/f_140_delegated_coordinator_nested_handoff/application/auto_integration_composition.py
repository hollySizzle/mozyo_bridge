"""The production composition of the #13686 actuator (Redmine #14825, items 2, 3 and 6).

#13686 built the machines and the ports; nothing wired them to anything real. This module is the
composition root: it binds the live Git adapter, the live durable authority reader, the durable
ledger and the live managed-process operations into one
:class:`~...application.auto_integration_actuator.AutoIntegrationUseCase`, so a success path
exists without a single test fake.

Three decisions here are the issue's, not incidental plumbing.

**The target branch is configured or the actuator refuses to exist** (item 6). The config record
described ``integration_branch: null`` as deferring to "runtime resolution", and no resolver was
ever written. Between building one and withdrawing the declaration, this takes the withdrawal,
and :func:`build_auto_integration_use_case` raises rather than composing an actuator with an
unresolved target. The reason is the one #13686 spent its review history on: the value in
question is the TARGET OF A PUSH, and a resolver would be a late-bound name standing in for an
identity — the exact shape that was removed from the merge (j#96406 finding 1), from the branch
delete (j#96396), and from the worktree removal (j#96401). "Fails closed rather than guessing" is
what the record already claimed; this makes the claim true at the only place that can enforce it.

**The asynchronous CI gate is a continuation, not a wait** (item 3). :class:`AsyncCiContinuation`
is what re-enters an action once its CI has settled, and it owns three things a caller would
otherwise improvise:

- *the owner* is whoever holds the ledger — the continuation performs no new authority, it
  re-runs the same action record against the same gates, and everything it can conclude the
  original run could have concluded had CI already settled;
- *the trigger* is a re-invocation with the SAME action record. It needs no separate state
  because the action key is the state: the durable ledger records which steps are done, and a
  re-run reads them back. That is why the ledger had to be durable before this could exist;
- *idempotency* is the action key plus the ledger's own uniqueness constraint. A duplicate
  trigger — two wakes, an operator re-run, a supervisor retry — re-reads and re-decides; it
  cannot re-push, because the push step is already recorded ``done`` and a second ``done`` for
  one step is refused by the store. A trigger that arrives while CI is still unsettled records
  nothing and says so.

**Nothing here decides.** Every gate is evaluated by the pure state machines through the ports
below. This module chooses which implementations answer, and choosing an implementation is not
an authority: an implementation that cannot answer leaves the gate closed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

import yaml

from mozyo_bridge.application.repo_local_config_loader import CONFIG_FILE_RELPATH
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (  # noqa: E501
    AutoIntegrationUseCase,
    IntegrationRunReport,
    integration_policy_from_config,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_admission_pin import (  # noqa: E501
    AdmittedActionPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    CI_STATE_FAILURE,
    CI_STATE_SUCCESS,
    CiStatusReader,
    GhCliCiStatusReader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    AutoIntegrationLedgerError,
    AutoIntegrationLedgerReader,
    SqliteLedgerStore,
    _open_ledger_writer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_authority import (  # noqa: E501
    LiveDurableAuthorityReader,
    live_lane_callback_scope,
    unresolved_lane_callback_debt,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (  # noqa: E501
    LiveAutoIntegrationGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_process_ops import (  # noqa: E501
    LiveManagedProcessOperations,
    ManagedInventoryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
    committed_config_policy_pointer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
    LiveRedmineJournalError,
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_authority import (  # noqa: E501
    LaneScope,
    fold_durable_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    AutoIntegrationPolicy,
    OUTCOME_DONE,
    OUTCOME_PENDING,
    PUSH_ACCEPTED,
    STATE_INTEGRATED,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    IntegrationActionRecord,
    completed_steps,
    is_full_sha,
    normalized_branch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    EvidenceJournal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
    resolve_journal_issuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (  # noqa: E501
    CleanupActionRecord,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (  # noqa: E501
    AutoIntegrationConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
)


class AutoIntegrationCompositionError(RuntimeError):
    """The production actuator could not be composed. Never a partially-wired actuator."""


# ---------------------------------------------------------------------------
# The target branch (item 6).
# ---------------------------------------------------------------------------


def declared_integration_branches(config: AutoIntegrationConfig) -> Tuple[str, ...]:
    """The integration branches the repository declares — one, or none at all.

    ``None`` is not a deferral to a runtime resolver. There is no runtime resolver, the
    declaration that there would be one is withdrawn by this issue, and an unset branch is
    therefore an unconfigured target: it matches no ``target_ref``, so
    ``target_identity_known`` stays False and every action fails closed before any mutation.
    """
    branch = normalized_branch(config.integration_branch or "")
    return (branch,) if branch else ()


# ---------------------------------------------------------------------------
# The live durable reads.
# ---------------------------------------------------------------------------


def live_journal_reader(
    *,
    repo_root: Path,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Callable[[str], Optional[Sequence[EvidenceJournal]]]:
    """A reader that fetches one issue's journals live and resolves each record's writer.

    The writer is resolved as POLICY from the note's own canonical gate structure, anchored to
    the COMMITTED config blob at ``HEAD`` — the workspace's existing binding, reused rather than
    re-decided here. An unresolvable policy pointer yields an empty anchor, which resolves every
    issuer to unknown and fails every authority closed: a binding that cannot name its own basis
    record binds nothing.

    A fetch that fails returns ``None``, not an empty page. "We could not read the evidence" and
    "the evidence says no" are different facts, and only one of them may look like an issue with
    nothing recorded on it.

    **The anchor is resolved per read, not once per closure** (review j#96611 finding 2). R1
    computed it when this reader was BUILT, so an actuator constructed before a config change
    kept resolving issuers against the blob that was committed at construction time — while the
    module docstring promised an action-time read. The anchor is what binds a writer to a role;
    reading it from a snapshot is exactly the stale-authority shape this subsystem removes
    everywhere else.
    """

    def read(issue: str) -> Optional[Sequence[EvidenceJournal]]:
        policy_pointer = committed_config_policy_pointer(repo_root)
        try:
            source = LiveRedmineJournalSource.from_environment(environ=environ, home=home)
            entries = source.read_entries(issue)
        except LiveRedmineJournalError:
            return None
        return tuple(
            EvidenceJournal(
                journal_id=str(entry.journal_id),
                notes=entry.notes or "",
                issuer=resolve_journal_issuer(
                    str(entry.journal_id),
                    entry.notes or "",
                    policy_pointer=policy_pointer,
                ),
                created_on=entry.created_on,
            )
            for entry in entries
        )

    return read


def live_issue_closed_reader(
    *,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Callable[[str], Optional[bool]]:
    """Whether the tracker itself reports the issue closed (``None`` when it cannot be asked).

    The tracker, not a ``close`` gate journal. A journal is the lane's statement that it believes
    itself finished; the issue status is the record that decides it, and the post-close cleanup
    is defined against the second one.
    """

    def read(issue: str) -> Optional[bool]:
        try:
            source = LiveRedmineJournalSource.from_environment(environ=environ, home=home)
            payload = source.transport(
                base_url=source.base_url,
                api_key=source.api_key,
                issue_id=str(issue),
                since=None,
            )
        except LiveRedmineJournalError:
            return None
        record = payload.get("issue") if isinstance(payload, Mapping) else None
        if not isinstance(record, Mapping):
            return None
        status = record.get("status")
        if not isinstance(status, Mapping):
            return None
        closed = status.get("is_closed")
        # Only an explicit boolean answers. A tracker projection that omits the field has not
        # said the issue is closed, and "absent" must not read as either value.
        return closed if isinstance(closed, bool) else None

    return read


def ledger_authorizing_action_reader(
    ledger: "AutoIntegrationLedgerReader",
) -> Callable[[CleanupActionRecord, str], str]:
    """Which integration action the LEDGER says put this lane's work on the target.

    The independent side of the cleanup's authorization check (item 5). The prefix is the action
    key's leading identity fields — issue, lane generation and source head, in that order — so a
    match is an identity constraint rather than a search.

    **The step asked for is the push, not the CI gate**, and the reason is a measured property of
    the state machine rather than a preference. The actuator records ``integration_ci`` only as
    ``pending``: when the evidence settles, the decision reads it and moves straight to its
    terminal state, so no ``done`` CI step is ever written. Keying on one would therefore have
    matched nothing in production while passing any test that wrote the entry by hand. The push
    is the durable ledger fact this reader actually needs — WHICH action published the commit —
    and whether CI then settled green is a separate conjunct that
    :attr:`CleanupAuthority.integration_ci_settled_green` answers from the durable record, so
    nothing is waived by asking the ledger the question the ledger can answer.

    A ``done`` push is additionally required to carry :data:`PUSH_ACCEPTED`. The actuator only
    records a ``done`` push on an accepted one, but this reader checks rather than relies on
    that: it is reading a file, and a file is not an invariant.

    **And the receipt must agree with a record its writer does not control** (review j#96650
    finding 1). R2 rested the authorization on the ledger alone, and a caller that took its own
    admission could write the row that satisfied it. ``proof_head`` is the commit the
    COORDINATOR's integration disposition says landed — read from the tracker, issuer-checked —
    and the ledger's push receipt must name exactly it. Forging the row is then not enough:
    the same actor would have to write a coordinator-issued marker on the issue as well, which
    is a different authority and not one the ledger's file permissions grant.

    ``ledger`` is a READ capability (:class:`AutoIntegrationLedgerReader`). A module that
    decides who authorized something has no business being able to write what it reads.

    Zero matches and more than one both answer ``""``. An empty key matches no record's, so the
    cleanup refuses — an ambiguous authorization is not an authorization.
    """

    def read(record: CleanupActionRecord, proof_head: str) -> str:
        prefix = "|".join(
            (
                f"issue={record.issue}",
                f"lane_generation={record.lane_generation}",
                f"source_head={record.recorded_source_head}",
                "",
            )
        )
        if not is_full_sha(proof_head):
            # No corroborating head means nothing to agree with, so nothing is authorized.
            return ""
        try:
            keys = ledger.completed_action_keys(prefix=prefix, step=STEP_PUSH)
            landed = [
                key for key in keys if _push_accepted(ledger, key, proof_head=proof_head)
            ]
        except AutoIntegrationLedgerError:
            return ""
        return landed[0] if len(landed) == 1 else ""

    return read


def _push_accepted(
    ledger: "AutoIntegrationLedgerReader", action_key: str, *, proof_head: str
) -> bool:
    """Whether this action's ``done`` push reports an accepted landing OF ``proof_head``."""
    return any(
        entry.step == STEP_PUSH
        and entry.outcome == OUTCOME_DONE
        and entry.push_status == PUSH_ACCEPTED
        and entry.head == proof_head
        for entry in ledger.read(action_key=action_key)
    )


# ---------------------------------------------------------------------------
# The composition root (item 2).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneBinding:
    """The lane this actuator IS — its own identity, supplied once at construction.

    Every ownership comparison the actuator makes is against these values, and none of them is
    ever taken from an action record. ``workspace`` / ``lane`` / ``lane_generation`` are the
    evidence envelope's side; ``issue`` / ``branch`` / ``worktree`` are the action record's side.
    """

    issue: str
    workspace: str
    lane: str
    lane_generation: int
    branch: str
    worktree: str


def build_auto_integration_use_case(
    *,
    binding: LaneBinding,
    config: AutoIntegrationConfig,
    repo_root: Path,
    lifecycle_store: Optional[LaneLifecycleStore] = None,
    inventory_ops: Optional[ManagedInventoryOps] = None,
    callback_outbox: object = None,
    admission_record: IntegrationActionRecord,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    ci_reader: Optional[CiStatusReader] = None,
) -> AutoIntegrationUseCase:
    """Compose the actuator against live ports, or refuse to compose one at all.

    ``inventory_ops`` and ``lifecycle_store`` are optional only so a caller that already holds
    them can pass them; omitting them binds the same live implementations the ``sublane
    hibernate`` path uses. ``callback_outbox`` omitted means the callback debt is unreadable,
    which leaves ``callbacks_drained`` False — an unwired port blocks, it does not waive.
    ``admission_record`` is required: before opening the durable writer,
    composition verifies that its review generation is the exact current approved
    ``review_request`` journal for its source head. There is no production composition path that
    can omit this check and later acquire mutation capability.
    """
    branches = declared_integration_branches(config)
    if not branches:
        raise AutoIntegrationCompositionError(
            "auto_integration.integration_branch is unset. This is an unconfigured target, not "
            "a deferral to runtime resolution: that declaration is withdrawn (Redmine #14825 "
            "item 6), because the value is the target of a push and resolving it from a "
            "late-bound name is the shape #13686 removed from every mutation it performs. "
            "Declare the branch in .mozyo-bridge/config.yaml."
        )

    scope = LaneScope(
        workspace=binding.workspace,
        lane=binding.lane,
        lane_generation=binding.lane_generation,
    )
    journals_fn = live_journal_reader(
        repo_root=repo_root, home=home, environ=environ
    )
    if (
        admission_record.issue != binding.issue
        or admission_record.lane_generation != binding.lane_generation
        or normalized_branch(admission_record.target_ref) not in branches
    ):
        raise AutoIntegrationCompositionError(
            "the action frame does not match this composition's issue, lane generation, "
            "and committed integration target"
        )
    journals = journals_fn(admission_record.issue)
    review = (
        fold_durable_authority(journals, scope=scope).review
        if journals is not None
        else None
    )
    current_generation = (
        review.request_journal
        if review is not None
        and review.admissible
        and review.head == admission_record.source_head
        else ""
    )
    if not current_generation or current_generation != admission_record.review_generation:
        # This runs before the ledger writer is opened. A caller-selected generation therefore
        # cannot create a fresh registry/ledger namespace merely by changing the action key.
        raise AutoIntegrationCompositionError(
            "the action's review generation is not the current approved review_request "
            "for its exact source head"
        )

    store = lifecycle_store or LaneLifecycleStore(home=home)
    ops = inventory_ops or _live_inventory_ops(repo_root=repo_root, environ=environ)
    # NOT injectable. R1 accepted a caller-supplied ledger for test convenience, which is the
    # very shape `LedgerStore`'s own docstring calls out — "handing the caller the ledger is the
    # same mistake as handing it the preflight" (review j#96611 finding 3). A test that needs a
    # scratch ledger passes `home`, which is what the production path uses to find one.
    durable_writer = _open_ledger_writer(home=home)
    durable_ledger = SqliteLedgerStore(home=home)
    authority = LiveDurableAuthorityReader(
        scope=scope,
        lane_issue=binding.issue,
        journals_fn=journals_fn,
        # Re-read from the repository on every call, NOT the tuple computed above (review
        # j#96611 finding 2). The construction-time value is what this actuator was pointed at;
        # `target_identity_known` asks what the repository declares NOW, and if those two ever
        # disagree the run must fail closed on the disagreement rather than integrate on its own
        # construction. A snapshot cannot express that, because it IS the construction.
        integration_branches_fn=lambda: _declared_branches_now(repo_root),
        callback_debt_fn=lambda: (
            unresolved_lane_callback_debt(
                callback_outbox,
                scope=live_lane_callback_scope(
                    store,
                    workspace_id=binding.workspace,
                    issue=binding.issue,
                    lane=binding.lane,
                    lane_generation=binding.lane_generation,
                ),
            )
            if callback_outbox is not None
            else None
        ),
        issue_closed_fn=live_issue_closed_reader(home=home, environ=environ),
        source_branch=binding.branch,
        ci_verdict_fn=(ci_reader or GhCliCiStatusReader(repo_root=repo_root)).verdict_for,
        authorizing_action_fn=ledger_authorizing_action_reader(
            AutoIntegrationLedgerReader(store=durable_ledger)
        ),
    )

    admitted_action_frame = (
        admission_record.action_key,
        binding.issue,
        binding.workspace,
        binding.lane,
        binding.lane_generation,
        binding.branch,
        binding.worktree,
        str(repo_root),
        admission_record.source_head,
        admission_record.target_ref,
        admission_record.expected_target_head,
        admission_record.review_generation,
    )
    admitted_cleanup_action_key = CleanupActionRecord(
        issue=binding.issue,
        lane_generation=binding.lane_generation,
        branch=binding.branch,
        worktree_path=binding.worktree,
        recorded_source_head=admission_record.source_head,
        integration_action_key=admission_record.action_key,
    ).action_key

    return AutoIntegrationUseCase(
        operations=LiveAutoIntegrationGitOperations(repo_root=repo_root),
        integration_policy=integration_policy_from_config(config),
        # And re-read before every decision (j#96650 finding 4). The constructor value above is
        # the starting point; this is what the gates actually consult.
        policy_source=lambda: _policy_now(repo_root),
        processes=LiveManagedProcessOperations(
            store=store,
            ops=ops,
            lane_workspace=binding.workspace,
            lane_id=binding.lane,
        ),
        authority=authority,
        ledger=durable_ledger,
        _ledger_writer=durable_writer,
        _admission_pin=AdmittedActionPin(
            action_frame=admitted_action_frame,
            cleanup_action_key=admitted_cleanup_action_key,
        ),
        lane_worktree=binding.worktree,
        lane_branch=binding.branch,
        lane_issue=binding.issue,
        lane_generation=binding.lane_generation,
    )


def _policy_now(repo_root: Path) -> AutoIntegrationPolicy:
    """The integration policy the repository declares AT THIS MOMENT (fail-closed).

    An unreadable config yields a policy whose ``mode`` is empty — outside the closed
    vocabulary, so the decision refuses it. That is the same direction as every other
    unreadable authority here: a config we cannot read has not authorized anything, and it
    certainly has not authorized what an earlier read of it said.
    """
    try:
        config = load_committed_repo_local_config(repo_root)
    except Exception:  # noqa: BLE001 — an unreadable config authorizes nothing
        return AutoIntegrationPolicy(mode="", integration_branch=None, ff_only=True)
    return integration_policy_from_config(config.auto_integration)


def _declared_branches_now(repo_root: Path) -> Tuple[str, ...]:
    """The integration branches the repository declares AT THIS MOMENT (fail-closed).

    A config that cannot be read declares nothing, so ``target_identity_known`` stays False and
    the action stops — an unreadable declaration is not a permissive one.
    """
    try:
        config = load_committed_repo_local_config(repo_root)
    except Exception:  # noqa: BLE001 — an unreadable config declares no target
        return ()
    return declared_integration_branches(config.auto_integration)


def load_committed_repo_local_config(repo_root: Path) -> RepoLocalConfig:
    """Parse the exact config blob at the repository's current ``HEAD``.

    The working-tree file is not authority: an uncommitted edit has passed neither review nor
    integration. Resolve ``HEAD`` once, then use that immutable object id for both tree lookup and
    blob read so a concurrent checkout cannot make this one observation span two commits. A
    committed tree with no config has the normal disabled default; an unreadable Git observation
    raises and its callers fail closed.
    """
    root = Path(repo_root)
    relpath = CONFIG_FILE_RELPATH.as_posix()

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AutoIntegrationCompositionError(
                "the committed auto-integration config could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        if proc.returncode != 0:
            raise AutoIntegrationCompositionError(
                "the committed auto-integration config could not be read from Git"
            )
        return proc

    head = git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if not is_full_sha(head):
        raise AutoIntegrationCompositionError(
            "the repository HEAD did not resolve to an exact commit"
        )
    names = tuple(
        line.strip()
        for line in git(
            "ls-tree", "--name-only", "--full-tree", head, "--", relpath
        ).stdout.splitlines()
        if line.strip()
    )
    if not names:
        return RepoLocalConfig.default()
    if names != (relpath,):
        raise AutoIntegrationCompositionError(
            "the committed auto-integration config path was ambiguous"
        )
    text = git("show", f"{head}:{relpath}").stdout
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AutoIntegrationCompositionError(
            "the committed auto-integration config was not valid YAML"
        ) from exc
    if parsed is None:
        return RepoLocalConfig.default()
    try:
        return RepoLocalConfig.from_record(parsed)
    except RepoLocalConfigError as exc:
        raise AutoIntegrationCompositionError(
            "the committed auto-integration config failed the closed schema"
        ) from exc


def _live_inventory_ops(
    *, repo_root: Path, environ: Optional[Mapping[str, str]]
) -> ManagedInventoryOps:
    """The same live herdr inventory / guarded close the ``sublane hibernate`` path uses."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
        LiveSublaneHibernateOps,
    )

    return LiveSublaneHibernateOps(
        repo_root=repo_root, env=dict(environ if environ is not None else os.environ)
    )


# ---------------------------------------------------------------------------
# The asynchronous CI continuation (item 3).
# ---------------------------------------------------------------------------

#: The continuation re-entered the action and CI had still not settled: nothing was recorded and
#: nothing changed. The correct response is to wait, not to retry harder.
CONTINUATION_CI_UNSETTLED = "ci_unsettled"
#: CI settled green on the exact landed commit and the action reached its terminal state.
CONTINUATION_INTEGRATED = "integrated"
#: The action has nothing to continue: no push receipt is recorded, so no CI gate is open.
CONTINUATION_NOT_AWAITING_CI = "not_awaiting_ci"
#: The action re-entered and stopped somewhere other than the CI gate — a gate that was open when
#: the continuation ran. Distinct from "still waiting", because waiting is not the problem.
CONTINUATION_BLOCKED = "blocked"
#: CI reached a terminal non-success conclusion. The action is not re-entered: a failed gate is
#: a terminal observation for this exact continuation frame, not an invitation to mutate.
CONTINUATION_CI_FAILED = "ci_failed"


@dataclass(frozen=True)
class ContinuationOutcome:
    """What one asynchronous continuation attempt established, as a typed status."""

    status: str
    state: str = ""
    landed_head: str = ""
    detail: str = ""
    report: Optional[IntegrationRunReport] = None


@dataclass(frozen=True)
class AsyncCiContinuation:
    """Re-enters an action whose CI gate was left pending, once that run has settled.

    It holds no state of its own. The action key IS the state — the durable ledger records which
    steps completed under it — so a continuation is exactly a re-run of
    :meth:`~...auto_integration_actuator.AutoIntegrationUseCase.run_integration` with the same
    record, and duplicate triggers are idempotent for the same reason a resume is: a step already
    recorded ``done`` is not offered again, and the store refuses a second ``done`` for one step
    even if something tried.

    What this adds over calling ``run_integration`` directly is the READING: which of the four
    things that can be true after a re-entry actually is, said as a value a caller can dispatch
    on rather than a report it must interpret.
    """

    use_case: AutoIntegrationUseCase

    def resume(self, record: IntegrationActionRecord) -> ContinuationOutcome:
        """Re-run ``record`` and classify what the re-entry established."""
        before = self.use_case.ledger.read(action_key=record.action_key)
        landed = completed_steps(
            before,
            action_key=record.action_key,
            recorded_by=self.use_case.recorder_id,
        ).get(STEP_PUSH)
        if landed is None:
            return ContinuationOutcome(
                status=CONTINUATION_NOT_AWAITING_CI,
                detail=(
                    "no push receipt is recorded under this action key, so there is no "
                    "integration SHA for a CI run to be about"
                ),
            )

        report = self.use_case.run_integration(record)
        state = report.final_decision.state if report.final_decision else ""
        # The TERMINAL STATE is the signal, not a ledger entry. The actuator records the CI step
        # `pending` and never `done`: once the evidence settles the decision reads it and moves
        # straight to `integrated`, performing no step. Classifying on a `done` CI entry would
        # have reported every real continuation as blocked (measured) while passing any test
        # that wrote that entry itself.
        if state == STATE_INTEGRATED:
            return ContinuationOutcome(
                status=CONTINUATION_INTEGRATED,
                state=state,
                landed_head=landed.head,
                detail="CI settled on the landed commit and the action reached its terminal state",
                report=report,
            )
        pending = any(
            outcome.step == STEP_INTEGRATION_CI and outcome.outcome == OUTCOME_PENDING
            for outcome in report.outcomes
        )
        if pending:
            return ContinuationOutcome(
                status=CONTINUATION_CI_UNSETTLED,
                state=state,
                landed_head=landed.head,
                detail=(
                    "the CI gate is still pending on the landed commit; this attempt recorded "
                    "no progress and none was available to record"
                ),
                report=report,
            )
        return ContinuationOutcome(
            status=CONTINUATION_BLOCKED,
            state=state,
            landed_head=landed.head,
            detail=(
                "the re-entered action stopped before the CI gate; a gate other than CI is open"
            ),
            report=report,
        )


@dataclass(frozen=True)
class CiSettlementTrigger:
    """The OWNER and TRIGGER of the asynchronous continuation (review j#96650 finding 3).

    R2 shipped :class:`AsyncCiContinuation` and a manual ``continue`` subcommand, and described
    the owner as "whoever holds the ledger". That is a description of an owner, not a binding:
    nothing observed CI reaching a terminal state and re-entered the action. This is the binding.

    - **owner** — this object. It holds the CI source and the use case, and it is the only thing
      that decides an action is ready to continue.
    - **trigger** — the CI provider reporting a TERMINAL state for the commit the push landed.
      Pending is not a trigger; unavailable is not a trigger. Both leave the action exactly
      where it was.
    - **idempotency** — the action key and the ledger, as before. Firing this twice re-reads and
      re-decides; it cannot re-push, because the push step is already recorded ``done`` and a
      second ``done`` is refused by the store. A repeated *unsettled* firing now also records
      nothing at all, which R2 claimed and did not do.

    It is a poll rather than a webhook because the durable state it needs — which action landed
    which commit — lives in the ledger, and a webhook would still have to look it up. What makes
    it a trigger rather than a timer is that it only continues an action whose CI has SETTLED.
    """

    use_case: AutoIntegrationUseCase
    ci_reader: CiStatusReader

    def settle(
        self,
        record: IntegrationActionRecord,
        *,
        workflow: str = "",
        attested_run: str = "",
        branch: str = "",
    ) -> ContinuationOutcome:
        """Continue ``record`` iff the CI for the commit it landed has reached a terminal state."""
        landed = completed_steps(
            self.use_case.ledger.read(action_key=record.action_key),
            action_key=record.action_key,
            recorded_by=self.use_case.recorder_id,
        ).get(STEP_PUSH)
        if landed is None or not landed.head:
            return ContinuationOutcome(
                status=CONTINUATION_NOT_AWAITING_CI,
                detail=(
                    "no push receipt names a landed commit under this action key, so there is "
                    "nothing for a CI run to be about"
                ),
            )
        verdict = self.ci_reader.verdict_for(
            landed.head,
            workflow=str(workflow or ""),
            attested_run=str(attested_run or ""),
            branch=normalized_branch(branch),
        )
        if verdict.state not in (CI_STATE_SUCCESS, CI_STATE_FAILURE):
            return ContinuationOutcome(
                status=CONTINUATION_CI_UNSETTLED,
                landed_head=landed.head,
                detail=(
                    f"CI for {landed.head} has not settled ({verdict.state}: {verdict.detail}); "
                    "the action is not re-entered and nothing is recorded"
                ),
            )
        if verdict.state == CI_STATE_FAILURE:
            return ContinuationOutcome(
                status=CONTINUATION_CI_FAILED,
                landed_head=landed.head,
                detail=(
                    f"CI for {landed.head} settled unsuccessfully ({verdict.detail}); "
                    "the action is not re-entered and no mutation is attempted"
                ),
            )
        return AsyncCiContinuation(use_case=self.use_case).resume(record)


__all__ = (
    "CiSettlementTrigger",
    "CONTINUATION_BLOCKED",
    "CONTINUATION_CI_FAILED",
    "CONTINUATION_CI_UNSETTLED",
    "CONTINUATION_INTEGRATED",
    "CONTINUATION_NOT_AWAITING_CI",
    "AsyncCiContinuation",
    "AutoIntegrationCompositionError",
    "ContinuationOutcome",
    "LaneBinding",
    "build_auto_integration_use_case",
    "declared_integration_branches",
    "ledger_authorizing_action_reader",
    "load_committed_repo_local_config",
    "live_issue_closed_reader",
    "live_journal_reader",
)
