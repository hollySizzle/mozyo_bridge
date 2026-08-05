"""The herdr session-start result model — per-slot outcomes + the run's aggregate.

The pure value layer of :mod:`herdr_session_start`: the slot-outcome vocabulary
(:data:`SLOT_ADOPTED` / :data:`SLOT_LAUNCHED` / :data:`SLOT_PLANNED` / :data:`SLOT_STALE` /
:data:`SLOT_UNATTESTED`) and the two records a run reports through (:class:`SlotResult`,
:class:`SessionStartResult`). No I/O, no subprocess, no decisions — just the shape of what
a session-start run returns, so the composition root keeps only the orchestration.

Homed here (Redmine #13646) as the session-start module's continuing module-health
reduction, alongside the pure decision core (:mod:`herdr_lane_topology`), the pure argv
assembly (:mod:`herdr_launch_argv`), and the side-effecting herdr commands
(:mod:`herdr_pane_lifecycle`). ``herdr_session_start`` re-exports every name here, so its
public surface — and every existing importer — is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_STALE as LIVENESS_STALE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    DISPOSITION_ADOPTED,
    DISPOSITION_FRESH_LAUNCHED,
    DISPOSITION_PLANNED,
    DISPOSITION_SURFACED,
    HEALTH_HEALTHY,
    HEALTH_NOT_PROBED,
)


# Per-slot outcome tokens.
SLOT_ADOPTED = "adopted"
SLOT_LAUNCHED = "launched"
SLOT_PLANNED = "planned"
# A host-restart shell / name residue: surfaced read-only (#13518 j#75329; see herdr_slot_liveness).
SLOT_STALE = LIVENESS_STALE
# A live slot whose startup self-attestation is absent / stale / missing / conflicting
# (Redmine #13637): the durable name matches a live agent, but its injected identity
# env is unverified, so it is surfaced read-only and never blind-adopted.
SLOT_UNATTESTED = "unattested"

#: The launch-disposition axis is *derived* from the outcome token, deliberately: the
#: outcome stays the single setter, so the two can never drift apart (Redmine #13948).
#: Total over the closed outcome vocabulary — an unmapped token raises rather than
#: defaulting, because a silently-defaulted disposition is how a wrong label survives.
_DISPOSITION_BY_OUTCOME: dict[str, str] = {
    SLOT_PLANNED: DISPOSITION_PLANNED,
    SLOT_ADOPTED: DISPOSITION_ADOPTED,
    SLOT_LAUNCHED: DISPOSITION_FRESH_LAUNCHED,
    # Both read-only surfacings of a PRE-EXISTING slot: this run neither planned, adopted,
    # nor started them, so neither is ever a rollback target.
    SLOT_STALE: DISPOSITION_SURFACED,
    SLOT_UNATTESTED: DISPOSITION_SURFACED,
}


@dataclass(frozen=True)
class _SlotPlan:
    """A per-provider decision (adopt / launch / dry-run plan) made before any launch.

    Classifying every slot up front lets the run pick a single launch-target
    workspace (and decide whether to create+reclaim a base pane) before it starts
    launching, so ``agent start`` can pass an explicit ``--workspace``.
    """

    provider: str
    assigned_name: str
    kind: str  # "adopt" | "launch" | "planned" | "stale" | "unattested"
    locator: str = ""  # adopted live locator (kind == "adopt") / stale residue pane (kind == "stale"); else ""
    detail: str = ""  # fail-closed reason for kind == "unattested" (Redmine #13637); else ""

@dataclass(frozen=True)
class SlotResult:
    """The outcome of preparing one provider slot's durable herdr identity.

    ``outcome`` says what this run *did* with the slot. It never said whether the thing
    it started came up — that is ``health``, observed after the fact (Redmine #13948,
    Answer j#80989): a slot is ``launched`` the moment ``agent start`` returns a locator,
    which is a claim by the launcher, not by the process. The axes are kept separate
    because collapsing them is the defect: there was previously nowhere to say "started,
    and nothing is running there".

    ``compensation`` records what this run owes for a side effect it already caused.
    session-start only ever reports it; the explicit public rollback rail is the only
    thing that may act on it (Answer j#80991).
    """

    provider: str
    assigned_name: str
    outcome: str
    locator: str = ""
    detail: str = ""
    #: The startup-health axis (:mod:`...domain.startup_health`). ``not_probed`` until the
    #: post-launch probe runs — and ``not_probed`` is NOT a success.
    health: str = HEALTH_NOT_PROBED
    #: The fixed provider-profile blocker token; non-empty only for a matched startup screen.
    blocker_id: str = ""
    #: The compensation axis. Only a fresh launch can ever owe one.
    compensation: str = COMPENSATION_NOT_NEEDED
    #: A fixed operator sentence for ``health`` (never observed pane content).
    health_detail: str = ""

    @property
    def disposition(self) -> str:
        """What this run did with the slot, on the closed disposition axis."""
        try:
            return _DISPOSITION_BY_OUTCOME[self.outcome]
        except KeyError:  # pragma: no cover - guards a vocabulary change, not a path
            raise ValueError(
                f"slot outcome {self.outcome!r} has no launch disposition; extend "
                "_DISPOSITION_BY_OUTCOME when the outcome vocabulary grows"
            ) from None

    @property
    def healthy(self) -> bool:
        """True only on a positively observed healthy slot."""
        return self.health == HEALTH_HEALTHY

    def as_payload(self) -> dict:
        return {
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "outcome": self.outcome,
            "locator": self.locator,
            "detail": self.detail,
            "disposition": self.disposition,
            "health": self.health,
            "blocker_id": self.blocker_id,
            "compensation": self.compensation,
            "health_detail": self.health_detail,
        }


@dataclass
class SessionStartResult:
    """The aggregate outcome of a session-start run.

    ``workspace_id`` / ``lane_id`` are the *mozyo* identities (registry anchor +
    requested lane). The base-pane fields (Redmine #13330) record the empty herdr
    root pane this run created and reclaimed on a pure cold start:

    - ``herdr_workspace_id`` — the herdr *terminal* workspace the launched agents
      live in (the one this run created, or the single workspace its adopted
      agents already occupy). Blank when nothing was launched.
    - ``base_pane_id`` — the ``root_pane.pane_id`` of the workspace this run
      **created** (blank when no workspace was created: all-adopt, dry-run, or a
      launch into an already-existing workspace). Only this exact pane is ever a
      reclaim target — never a scanned-for shell (fail-closed against closing a
      user's own shell).
    - ``base_pane_reclaimed`` — True iff that created root pane was closed.
    - ``base_pane_detail`` — a non-fatal ``pane close`` failure detail, if any
      (a failed reclaim leaves harmless cosmetic residue, never a hard failure).

    The tab fields (Redmine #13411) are the lane=tab analogue: a non-default lane
    lands in its OWN dedicated herdr tab inside the sublane host workspace, its
    gateway + worker split inside it. The default lane never uses a tab, so these
    stay blank for it (byte-invariant coordinator path):

    - ``herdr_tab_id`` — the herdr tab the launched lane agents live in (the one
      this run created, or the tab its adopted slots already occupy). Blank for
      the default lane / all-adopt / nothing launched.
    - ``tab_pane_id`` — the ``root_pane.pane_id`` of the tab this run **created**
      (blank when no tab was created: default lane, all-adopt, or a heal that
      rejoined an existing tab). Only this exact pane is ever a reclaim target.
    - ``tab_pane_reclaimed`` — True iff that created tab root pane was closed.
    - ``tab_pane_detail`` — a non-fatal tab root ``pane close`` failure detail.
    """

    workspace_id: str
    lane_id: str
    slots: list = field(default_factory=list)
    herdr_workspace_id: str = ""
    base_pane_id: str = ""
    base_pane_reclaimed: bool = False
    base_pane_detail: str = ""
    herdr_tab_id: str = ""
    tab_pane_id: str = ""
    tab_pane_reclaimed: bool = False
    tab_pane_detail: str = ""
    #: This run only planned (``--dry-run``): it started nothing, so there is nothing to
    #: observe and nothing to compensate. A plan is reported successful on its own terms.
    dry_run: bool = False
    #: The immutable startup-transaction identity this run reserved before its first side
    #: effect (Redmine #13948). Blank for a dry run. It is the ONLY handle an explicit
    #: rollback/replay may act under, so it is surfaced for the operator to pass back.
    action_id: str = ""
    #: The declared pair-split-ratio axis (Redmine #14569) — one of
    #: :data:`...herdr_pair_split_ratio.RATIO_OUTCOMES`. Deliberately its own axis rather
    #: than folded into a slot's ``health``: the ratio is a property of the DIVIDER between
    #: two panes, not of either agent, and a pair can be perfectly healthy while wrongly
    #: divided. ``not_applicable`` is the resting value — a run that created no divider of
    #: its own never claims anything about one.
    ratio_outcome: str = "not_applicable"
    #: The measurement (or the refusal) behind :attr:`ratio_outcome`: declared vs observed
    #: ratio and first-pane extent on a success, the fixed reason on a failure / deferral.
    ratio_detail: str = ""
    #: The project-column axis (Redmine #14996 R2) — one of
    #: :data:`...herdr_project_column_reflow.COLUMN_OUTCOMES`. Its own axis for the same
    #: reason the ratio is: a column is a property of how the shared coordinator TAB is
    #: divided between projects, not of any one agent, and every pair can be healthy while
    #: one project's pair spans another's. ``not_applicable`` is the resting value — every
    #: launch path except a fresh pair appended to an occupied shared project-coordinator
    #: workspace never claims anything about a column.
    column_outcome: str = "not_applicable"
    #: The measurement (or the refusal) behind :attr:`column_outcome`. On a failure it names
    #: the refusing step and any pane left outside the shared tab.
    column_detail: str = ""

    @property
    def column_ok(self) -> bool:
        """True only for a RECOGNISED, non-failing project-column outcome.

        Membership in the closed success vocabulary, not ``!= failed`` — the same
        discipline :attr:`ratio_ok` adopted after the negative comparison reported every
        unreadable token as a success (review j#91418 R5-F1).
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E501
            COLUMN_SUCCESS_OUTCOMES,
        )

        return self.column_outcome in COLUMN_SUCCESS_OUTCOMES

    @property
    def ratio_ok(self) -> bool:
        """True only for a RECOGNISED, non-failing pair-split-ratio outcome.

        Membership in the closed success vocabulary, not ``!= failed`` (review j#91418
        R5-F1). The negative comparison reported every unrecognised token as a success — a
        producer typo, a case variant, a truncation, an empty string — so a run whose ratio
        verdict nobody could interpret still exited 0. An outcome this axis cannot read is
        not evidence that the pair was divided correctly, which is the same reason
        :attr:`SlotResult.disposition` refuses an unknown slot outcome rather than
        defaulting one.

        ``deferred_until_full_relaunch`` IS in the success set, deliberately: it is the
        honest, documented outcome of an order-deferred heal, in which applying the ratio
        would put ``order[0]``'s share on the wrong provider and the alternative — moving a
        live pane — is forbidden. The run says what it did not do; it did not do the wrong
        thing. The unknown token stays on :attr:`ratio_outcome` (and so in
        :meth:`as_payload`) so the reader can see WHICH token was unreadable.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
            RATIO_SUCCESS_OUTCOMES,
        )

        return self.ratio_outcome in RATIO_SUCCESS_OUTCOMES

    @property
    def ok(self) -> bool:
        """Fully successful iff every requested role is healthy AND the pair is as declared.

        This is the contract the defect violated: success used to mean "``agent start``
        was accepted for every slot", so a pair whose Claude exec'd and exited instantly
        still exited 0 (#13882 j#80951 / j#80968). It now means every requested role is
        live at the locator we launched, screen-clear, and locator-matched self-attested.

        A read-only surfacing (``stale`` / ``unattested``) is deliberately NOT success:
        the pair is not usable, and saying so is the point of the issue.

        Redmine #14569 adds the second conjunct: a declared pair split ratio that was owed
        and could not be established is not success either (Design Answer j#91127, "ratio
        適用失敗を成功扱いしない"). It stays out of :attr:`owes_rollback` on purpose —
        a mis-divided pair is fully usable, so nothing is owed a teardown for it.

        Redmine #14996 R2 adds the third on exactly the same terms (j#99845, "途中失敗は
        typed fail-closed"): a project column that was owed and could not be established —
        including one that left a pane outside the shared tab — is not success. It is
        likewise no rollback debt: every agent is live and correctly routed.
        """
        if self.dry_run:
            # Nothing was started, so no health claim is made or needed: the deliverable
            # of a dry run is the plan itself.
            return True
        return (
            bool(self.slots)
            and all(slot.healthy for slot in self.slots)
            and self.ratio_ok
            and self.column_ok
        )

    @property
    def owes_rollback(self) -> bool:
        """True iff a slot THIS run freshly launched did not come up healthy (a debt).

        Deliberately narrower than ``not ok`` (Redmine #13933 R13, j#82038): an *adopted*
        or read-only *surfaced* slot that is unhealthy makes the pair unusable (``ok`` is
        False) but is NOT this run's compensation debt — the run never launched it, so there
        is nothing for the explicit rollback rail to undo. Only a fresh launch can ever owe a
        compensation (the ``compensation`` axis is set that way in :mod:`...startup_health`),
        so the startup transaction settles its debt on THIS, not on the pair aggregate. The
        old ``settle(ok=result.ok)`` over-owed: a healthy fresh target left a rollback owed
        only because a non-green sibling it adopted dragged the aggregate false, which is the
        v1 replacement bind that stalled at ``launch_owed`` in the installed a14 dogfood.
        """
        return any(
            slot.compensation == COMPENSATION_ROLLBACK_OWED for slot in self.slots
        )

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "action_id": self.action_id,
            "slots": [slot.as_payload() for slot in self.slots],
            "herdr_workspace_id": self.herdr_workspace_id,
            "base_pane_id": self.base_pane_id,
            "base_pane_reclaimed": self.base_pane_reclaimed,
            "base_pane_detail": self.base_pane_detail,
            "herdr_tab_id": self.herdr_tab_id,
            "tab_pane_id": self.tab_pane_id,
            "tab_pane_reclaimed": self.tab_pane_reclaimed,
            "tab_pane_detail": self.tab_pane_detail,
            "ratio_outcome": self.ratio_outcome,
            "ratio_detail": self.ratio_detail,
            "column_outcome": self.column_outcome,
            "column_detail": self.column_detail,
        }


__all__ = (
    "SLOT_ADOPTED",
    "SLOT_LAUNCHED",
    "SLOT_PLANNED",
    "SLOT_STALE",
    "SLOT_UNATTESTED",
    "SessionStartResult",
    "SlotResult",
    "_SlotPlan",
)
