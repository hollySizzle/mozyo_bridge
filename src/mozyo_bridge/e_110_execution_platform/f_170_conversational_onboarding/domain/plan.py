"""Drift-bound onboarding plan + human gate receipt (Redmine #13498 / #13501).

The plan binds a concrete, human-confirmable mutation sequence to the *exact*
facts it was built from. ``onboarding.plan`` re-runs the deterministic inspect
itself and builds the plan from those facts — it never accepts model-supplied
path / risk / adoption / binary facts. ``onboarding.apply`` re-derives the
fingerprint from freshly re-inspected facts and refuses to run a plan whose
fingerprint no longer matches (drift), so a tree that changed between plan and
apply cannot be mutated under a stale plan.

The **human gate receipt** is an opaque token the CLI / orchestrator issues
*after* obtaining the human's caution acknowledgement, before any model runs. It
is an HMAC over the root fingerprint and the path risk keyed by a secret the
model never sees, so the model cannot forge one or rebind an ack to a different
root. The planner verifies it and refuses to plan a ``caution_requires_ack``
root without a valid receipt.

Everything here is pure (hashing + validation); no filesystem, env, or clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .intent import GIT_MODE_INITIALIZE, OnboardingIntent
from .path_safety import PATH_RISK_SYNC_OR_CLOUD, ROOT_KIND_GIT
from .preflight import (
    STATE_ADOPTED,
    STATE_ADOPTION_IN_PROGRESS,
    STATE_BLOCKED,
    STATE_CAUTION_REQUIRES_ACK,
    STATE_UNADOPTED,
)
from .receipt import ORDERED_STEPS

__all__ = (
    "PRESET_INTENT_TO_SCAFFOLD",
    "OnboardingFacts",
    "PlanStep",
    "OnboardingPlan",
    "PlanError",
    "issue_human_gate_receipt",
    "verify_human_gate_receipt",
    "compute_root_fingerprint",
    "compute_plan_id",
    "build_plan",
    "parse_plan_record",
)

# Conversation preset enum (underscored) → scaffold preset name (hyphenated).
# ``undecided`` has no scaffold preset and cannot be planned.
PRESET_INTENT_TO_SCAFFOLD: dict[str, str] = {
    "none": "none",
    "asana": "asana",
    "redmine": "redmine",
    "redmine_governed": "redmine-governed",
    "redmine_rails": "redmine-rails",
    "redmine_rails_governed": "redmine-rails-governed",
}

_HUMAN_GATE_PREFIX = "hgr.v1."


class PlanError(Exception):
    """A structured, coded reason a plan cannot be built for the current facts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_record(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class OnboardingFacts:
    """The re-inspected, model-independent facts a plan is bound to.

    ``existing_file_hashes`` maps repo-relative adoption-relevant paths (config,
    scaffold manifest, receipt) to their sha256 hex; a missing file is simply
    absent from the map. Any change to these between plan and apply flips the
    fingerprint, which is how drift is detected.

    ``herdr_binary_realpath`` is carried for display / verification but is
    deliberately **excluded** from the drift fingerprint (design-compliance note
    for #13501): it is an *environment* fact, not a tree fact, and the launch
    binary is always re-resolved from the trusted env at launch time (the real
    security boundary), so folding it into a tree-drift check would make plans
    spuriously drift across otherwise-equivalent invocations. A missing / moved
    binary is caught at the verify step, not as plan drift.
    """

    canonical_root: str
    state: str
    root_kind: str
    path_risk: str
    adoption_marker: str
    herdr_binary_realpath: str | None
    existing_file_hashes: Mapping[str, str] = field(default_factory=dict)

    def fingerprint_material(self) -> dict[str, object]:
        return {
            "canonical_root": self.canonical_root,
            "root_kind": self.root_kind,
            "path_risk": self.path_risk,
            "adoption_marker": self.adoption_marker,
            "existing_file_hashes": dict(sorted(self.existing_file_hashes.items())),
        }


@dataclass(frozen=True)
class PlanStep:
    """One visible, human-confirmable mutation step."""

    step_id: str
    summary: str


@dataclass(frozen=True)
class OnboardingPlan:
    """A drift-bound onboarding plan the human confirms before apply."""

    plan_id: str
    root_fingerprint: str
    canonical_root: str
    scaffold_preset: str
    rules_store: str
    ordered_steps: tuple[PlanStep, ...]
    warnings: tuple[str, ...] = ()
    requires_confirmation: bool = True

    def as_record(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "root_fingerprint": self.root_fingerprint,
            "canonical_root": self.canonical_root,
            "scaffold_preset": self.scaffold_preset,
            "rules_store": self.rules_store,
            "ordered_steps": [
                {"step_id": s.step_id, "summary": s.summary} for s in self.ordered_steps
            ],
            "warnings": list(self.warnings),
            "requires_confirmation": self.requires_confirmation,
        }


def _digest(material: object) -> str:
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def issue_human_gate_receipt(
    root_fingerprint: str, path_risk: str, *, secret: str
) -> str:
    """Issue an opaque human-gate receipt bound to ``root_fingerprint`` + risk.

    Called by the CLI / orchestrator after the human acks the caution — never by
    the model. Keyed by ``secret`` (held outside the model) so it cannot be
    forged or rebound to another root.
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{root_fingerprint}\x1f{path_risk}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_HUMAN_GATE_PREFIX}{mac}"


def verify_human_gate_receipt(
    token: str | None, root_fingerprint: str, path_risk: str, *, secret: str
) -> bool:
    """Constant-time verify a human-gate receipt against the current facts."""
    if not token or not isinstance(token, str) or not token.startswith(_HUMAN_GATE_PREFIX):
        return False
    expected = issue_human_gate_receipt(root_fingerprint, path_risk, secret=secret)
    return hmac.compare_digest(token, expected)


def compute_root_fingerprint(facts: OnboardingFacts) -> str:
    """Deterministic fingerprint over the drift-relevant facts of the root."""
    return _digest(facts.fingerprint_material())


def compute_plan_id(
    root_fingerprint: str,
    canonical_root: str,
    scaffold_preset: str,
    rules_store: str,
    step_ids: Sequence[str],
) -> str:
    """Deterministic plan id over the stable, executable fields of a plan.

    Deliberately a function of only what the runner executes (root identity +
    fingerprint, scaffold preset, rules store, ordered steps) so ``apply`` can
    recompute it from a serialized plan record and detect tampering. The intent
    fields that gate *whether* a plan is built (``action`` / ``git_mode``) do not
    change the executed steps, so they are excluded.
    """
    return _digest(
        {
            "root_fingerprint": root_fingerprint,
            "canonical_root": canonical_root,
            "scaffold_preset": scaffold_preset,
            "rules_store": rules_store,
            "steps": list(step_ids),
        }
    )


def _ordered_plan_steps(scaffold_preset: str, rules_store: str) -> tuple[PlanStep, ...]:
    summaries = {
        "onboarding_receipt": "record an onboarding receipt (adoption_in_progress)",
        "scaffold_apply": f"apply scaffold preset '{scaffold_preset}' (--backup)",
        "config_write_once": "write-once .mozyo-bridge/config.yaml (herdr backend)",
        "rules_install": f"install rules into the {rules_store} store",
        "workspace_register": "register the workspace",
        "verify": "verify scaffold status / config / workspace / herdr preflight",
        "finalize": "mark receipt complete and run the bare-launch readiness check",
    }
    return tuple(PlanStep(step_id=step, summary=summaries[step]) for step in ORDERED_STEPS)


def build_plan(
    facts: OnboardingFacts,
    intent: OnboardingIntent,
    *,
    human_gate_receipt: str | None = None,
    gate_secret: str,
) -> OnboardingPlan:
    """Build a drift-bound plan from re-inspected ``facts`` and a valid ``intent``.

    Fails closed with :class:`PlanError` when the state / intent forbids planning:
    a blocked root, an already-adopted root, an undecided preset, a
    ``caution_requires_ack`` root without a valid human-gate receipt, or a
    ``git_mode=initialize`` on a sync/cloud root (the standing invariant that
    ``git init`` is always refused on synced folders).
    """
    if facts.state == STATE_BLOCKED:
        raise PlanError("blocked", "root is a hard block; no plan can be built")
    if facts.state in (STATE_ADOPTED, STATE_ADOPTION_IN_PROGRESS):
        raise PlanError(
            "not_plannable",
            f"root state {facts.state!r} is not a plannable fresh adoption "
            "(already adopted or resume-in-progress)",
        )

    if intent.preset_undecided:
        raise PlanError(
            "preset_undecided",
            "preset is undecided; ask the human to choose a preset before planning",
        )
    scaffold_preset = PRESET_INTENT_TO_SCAFFOLD.get(intent.preset)
    if scaffold_preset is None:
        raise PlanError("unknown_preset", f"no scaffold preset for {intent.preset!r}")

    # Standing invariant: git init is always refused on a sync/cloud folder.
    if (
        intent.git_mode == GIT_MODE_INITIALIZE
        and facts.path_risk == PATH_RISK_SYNC_OR_CLOUD
    ):
        raise PlanError(
            "git_init_forbidden_on_sync",
            "git_mode=initialize is always refused on a sync/cloud folder",
        )

    fingerprint = compute_root_fingerprint(facts)

    # A caution root requires a valid human-gate receipt bound to these facts.
    if facts.state == STATE_CAUTION_REQUIRES_ACK:
        if not verify_human_gate_receipt(
            human_gate_receipt, fingerprint, facts.path_risk, secret=gate_secret
        ):
            raise PlanError(
                "human_gate_required",
                "sync/cloud root requires a valid human caution acknowledgement "
                "receipt bound to this root before a plan can be built",
            )

    # git_mode=initialize (even on a normal root) demands independent human
    # confirmation; it is out of MVP scope, so require the same caution receipt.
    if intent.git_mode == GIT_MODE_INITIALIZE:
        if not verify_human_gate_receipt(
            human_gate_receipt, fingerprint, facts.path_risk, secret=gate_secret
        ):
            raise PlanError(
                "git_init_requires_confirmation",
                "git_mode=initialize requires an independent human confirmation "
                "receipt bound to this root",
            )

    warnings: list[str] = []
    if facts.path_risk == PATH_RISK_SYNC_OR_CLOUD:
        warnings.append(
            "root is on a sync/cloud folder; it is adopted as non-Git and git "
            "init is refused"
        )
    if facts.root_kind != ROOT_KIND_GIT:
        warnings.append("root is not a Git worktree; adopted as a non-Git workspace")

    steps = _ordered_plan_steps(scaffold_preset, intent.rules_store)
    plan_id = compute_plan_id(
        fingerprint,
        facts.canonical_root,
        scaffold_preset,
        intent.rules_store,
        [s.step_id for s in steps],
    )

    return OnboardingPlan(
        plan_id=plan_id,
        root_fingerprint=fingerprint,
        canonical_root=facts.canonical_root,
        scaffold_preset=scaffold_preset,
        rules_store=intent.rules_store,
        ordered_steps=steps,
        warnings=tuple(warnings),
        requires_confirmation=True,
    )


def parse_plan_record(record: Mapping[str, object]) -> OnboardingPlan:
    """Rebuild an :class:`OnboardingPlan` from its serialized record.

    Fails closed with :class:`PlanError` (``malformed_plan`` / ``tampered_plan``)
    when the record is not a well-formed plan or when its ``plan_id`` does not
    match a recomputation from the plan's own executable fields — the integrity
    check ``apply`` relies on so a hand-edited plan cannot be run.
    """
    if not isinstance(record, Mapping):
        raise PlanError("malformed_plan", "plan record must be a mapping")
    try:
        plan_id = str(record["plan_id"])
        root_fingerprint = str(record["root_fingerprint"])
        canonical_root = str(record["canonical_root"])
        scaffold_preset = str(record["scaffold_preset"])
        rules_store = str(record["rules_store"])
        raw_steps = record["ordered_steps"]
    except KeyError as exc:
        raise PlanError("malformed_plan", f"plan record missing field {exc}") from exc

    if not isinstance(raw_steps, (list, tuple)):
        raise PlanError("malformed_plan", "plan ordered_steps must be a list")
    steps: list[PlanStep] = []
    for entry in raw_steps:
        if not isinstance(entry, Mapping) or "step_id" not in entry:
            raise PlanError("malformed_plan", "plan step must have a step_id")
        steps.append(
            PlanStep(step_id=str(entry["step_id"]), summary=str(entry.get("summary", "")))
        )

    expected = compute_plan_id(
        root_fingerprint,
        canonical_root,
        scaffold_preset,
        rules_store,
        [s.step_id for s in steps],
    )
    if not hmac.compare_digest(plan_id, expected):
        raise PlanError(
            "tampered_plan",
            "plan_id does not match the plan's executable fields; refusing to "
            "apply a tampered plan",
        )

    warnings = record.get("warnings", [])
    return OnboardingPlan(
        plan_id=plan_id,
        root_fingerprint=root_fingerprint,
        canonical_root=canonical_root,
        scaffold_preset=scaffold_preset,
        rules_store=rules_store,
        ordered_steps=tuple(steps),
        warnings=tuple(str(w) for w in warnings) if isinstance(warnings, (list, tuple)) else (),
        requires_confirmation=bool(record.get("requires_confirmation", True)),
    )
