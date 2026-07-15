"""Drift-bound, authority-bound onboarding plan + human gate receipt (#13498 / #13501).

The plan is the single authority for what ``apply`` may mutate, and that
authority is a **trusted-secret HMAC**, not a caller-recomputable hash (Redmine
#13501 review F2). ``plan_id`` is an HMAC — keyed by a secret the model / caller
never sees — over the *entire* binding the spec requires (spec lines 107-110):
the canonical root, the re-inspected preflight facts (state + tree fingerprint +
existing file hashes + herdr binary realpath), every closed ``OnboardingIntent``
field, the required human gate receipt, and the exact ordered steps / preset /
store. Because it is keyed, a caller cannot forge a plan for altered content.

``onboarding.apply`` never trusts a caller-supplied ``preset`` / ``store`` /
``ordered_steps``: it re-inspects fresh facts, **rebuilds** the authoritative
plan deterministically from the supplied closed intent + receipt, recomputes the
HMAC over the fresh facts, and only proceeds when it equals the supplied
``plan_id`` (:func:`rebuild_and_verify_plan`). A forged, recomputed, drifted, or
wrong-secret plan all fail the same check. The executed steps are the rebuilt
authoritative steps, so what the human confirmed is exactly what is mutated.

The **human gate receipt** is an HMAC over the root fingerprint + risk, keyed by
the same trusted secret. Issuing or verifying with an empty / missing secret is
**refused** (Redmine #13501 review F1) so a gate can never be bypassed with an
empty-key HMAC.

Everything here is pure (hashing + validation); no filesystem, env, or clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .intent import (
    GIT_MODE_INITIALIZE,
    OnboardingIntent,
    validate_onboarding_intent,
)
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
    "require_gate_secret",
    "issue_human_gate_receipt",
    "verify_human_gate_receipt",
    "compute_root_fingerprint",
    "build_plan",
    "rebuild_and_verify_plan",
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

_HUMAN_GATE_PREFIX = "hgr.v2."
_PLAN_PREFIX = "plan.v2."


class PlanError(Exception):
    """A structured, coded reason a plan cannot be built / verified."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_record(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message}


def require_gate_secret(secret: object) -> str:
    """Return a non-empty trusted gate secret, or fail closed.

    The onboarding gate authority (human gate receipt + plan_id HMAC) is only as
    strong as this secret, so an unset / empty / whitespace-only / non-string
    secret is refused rather than degrading to a weak-key HMAC anyone could
    reproduce (Redmine #13501 review F1 / j#74844).
    """
    if not isinstance(secret, str) or not secret.strip():
        raise PlanError(
            "gate_secret_required",
            "a non-empty (non-whitespace) trusted gate secret "
            "(MOZYO_ONBOARDING_GATE_SECRET) is required; refusing to operate the "
            "human gate / plan authority with an empty key",
        )
    return secret


@dataclass(frozen=True)
class OnboardingFacts:
    """The re-inspected, model-independent facts a plan is bound to."""

    canonical_root: str
    state: str
    root_kind: str
    path_risk: str
    adoption_marker: str
    herdr_binary_realpath: str | None
    existing_file_hashes: Mapping[str, str] = field(default_factory=dict)

    def fingerprint_material(self) -> dict[str, object]:
        # Tree-drift fingerprint: the on-disk facts that must not change between
        # plan and apply. The herdr binary realpath is intentionally excluded
        # here (it is an environment fact re-resolved at launch) — but it IS
        # bound into the authoritative plan_id below, per spec lines 107-110.
        return {
            "canonical_root": self.canonical_root,
            "root_kind": self.root_kind,
            "path_risk": self.path_risk,
            "adoption_marker": self.adoption_marker,
            "existing_file_hashes": dict(sorted(self.existing_file_hashes.items())),
        }


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    summary: str


@dataclass(frozen=True)
class OnboardingPlan:
    """A drift-bound, authority-bound onboarding plan the human confirms."""

    plan_id: str
    root_fingerprint: str
    canonical_root: str
    intent: OnboardingIntent
    human_gate_receipt: str | None
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
            "intent": _intent_record(self.intent),
            "human_gate_receipt": self.human_gate_receipt,
            "scaffold_preset": self.scaffold_preset,
            "rules_store": self.rules_store,
            "ordered_steps": [
                {"step_id": s.step_id, "summary": s.summary} for s in self.ordered_steps
            ],
            "warnings": list(self.warnings),
            "requires_confirmation": self.requires_confirmation,
        }


def _intent_record(intent: OnboardingIntent) -> dict[str, object]:
    return {
        "schema_version": intent.schema_version,
        "action": intent.action,
        "preset": intent.preset,
        "backend": intent.backend,
        "git_mode": intent.git_mode,
        "rules_store": intent.rules_store,
        "free_text_summary": intent.free_text_summary,
    }


def _digest(material: object) -> str:
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sign(secret: str, material: object) -> str:
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_human_gate_receipt(
    root_fingerprint: str, path_risk: str, *, secret: str
) -> str:
    """Issue an opaque human-gate receipt bound to ``root_fingerprint`` + risk.

    Refuses an empty / missing secret (Redmine #13501 review F1).
    """
    secret = require_gate_secret(secret)
    mac = _sign(secret, {"kind": "human_gate", "fp": root_fingerprint, "risk": path_risk})
    return f"{_HUMAN_GATE_PREFIX}{mac}"


def verify_human_gate_receipt(
    token: object, root_fingerprint: str, path_risk: str, *, secret: object
) -> bool:
    """Constant-time verify a human-gate receipt; ``False`` on an empty secret."""
    if not isinstance(secret, str) or not secret.strip():
        return False
    if not isinstance(token, str) or not token.startswith(_HUMAN_GATE_PREFIX):
        return False
    expected = issue_human_gate_receipt(root_fingerprint, path_risk, secret=secret)
    return hmac.compare_digest(token, expected)


def compute_root_fingerprint(facts: OnboardingFacts) -> str:
    """Deterministic tree-drift fingerprint over the on-disk facts of the root."""
    return _digest(facts.fingerprint_material())


def _ordered_plan_steps(scaffold_preset: str, rules_store: str) -> tuple[PlanStep, ...]:
    summaries = {
        "onboarding_receipt": "record an onboarding receipt (adoption_in_progress)",
        "rules_install": f"install rules into the {rules_store} store",
        "scaffold_apply": f"apply scaffold preset '{scaffold_preset}' (--backup)",
        "config_write_once": "write-once .mozyo-bridge/config.yaml (herdr backend)",
        "workspace_register": "register the workspace",
        "verify": "verify scaffold status / config / workspace / herdr preflight",
        "finalize": "mark the onboarding receipt complete",
    }
    return tuple(PlanStep(step_id=step, summary=summaries[step]) for step in ORDERED_STEPS)


def _authoritative_plan_id(
    secret: str,
    facts: OnboardingFacts,
    intent: OnboardingIntent,
    human_gate_receipt: str | None,
    scaffold_preset: str,
    rules_store: str,
    step_ids: Sequence[str],
) -> str:
    """The keyed HMAC authority token binding the full plan (spec 107-110)."""
    material = {
        "canonical_root": facts.canonical_root,
        "root_fingerprint": compute_root_fingerprint(facts),
        "state": facts.state,
        "adoption_marker": facts.adoption_marker,
        "herdr_binary_realpath": facts.herdr_binary_realpath,
        "existing_file_hashes": dict(sorted(facts.existing_file_hashes.items())),
        "intent": _intent_record(intent),
        "human_gate_receipt": human_gate_receipt or "",
        "scaffold_preset": scaffold_preset,
        "rules_store": rules_store,
        "ordered_steps": list(step_ids),
    }
    return f"{_PLAN_PREFIX}{_sign(secret, material)}"


def build_plan(
    facts: OnboardingFacts,
    intent: OnboardingIntent,
    *,
    human_gate_receipt: str | None = None,
    gate_secret: str,
) -> OnboardingPlan:
    """Build an authority-bound plan from re-inspected ``facts`` and ``intent``.

    Requires a non-empty ``gate_secret`` (F1). Fails closed with
    :class:`PlanError` when the state / intent forbids planning (blocked,
    already-adopted, undecided preset, ``caution_requires_ack`` without a valid
    human-gate receipt, or ``git_mode=initialize`` on sync/cloud or without an
    independent confirmation receipt).
    """
    secret = require_gate_secret(gate_secret)

    if facts.state == STATE_BLOCKED:
        raise PlanError("blocked", "root is a hard block; no plan can be built")
    if facts.state in (STATE_ADOPTED, STATE_ADOPTION_IN_PROGRESS):
        raise PlanError(
            "not_plannable",
            f"root state {facts.state!r} is not a plannable fresh adoption",
        )

    if intent.preset_undecided:
        raise PlanError(
            "preset_undecided",
            "preset is undecided; ask the human to choose a preset before planning",
        )
    scaffold_preset = PRESET_INTENT_TO_SCAFFOLD.get(intent.preset)
    if scaffold_preset is None:
        raise PlanError("unknown_preset", f"no scaffold preset for {intent.preset!r}")

    if (
        intent.git_mode == GIT_MODE_INITIALIZE
        and facts.path_risk == PATH_RISK_SYNC_OR_CLOUD
    ):
        raise PlanError(
            "git_init_forbidden_on_sync",
            "git_mode=initialize is always refused on a sync/cloud folder",
        )

    fingerprint = compute_root_fingerprint(facts)

    if facts.state == STATE_CAUTION_REQUIRES_ACK:
        if not verify_human_gate_receipt(
            human_gate_receipt, fingerprint, facts.path_risk, secret=secret
        ):
            raise PlanError(
                "human_gate_required",
                "sync/cloud root requires a valid human caution acknowledgement "
                "receipt bound to this root before a plan can be built",
            )

    if intent.git_mode == GIT_MODE_INITIALIZE:
        if not verify_human_gate_receipt(
            human_gate_receipt, fingerprint, facts.path_risk, secret=secret
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
    plan_id = _authoritative_plan_id(
        secret,
        facts,
        intent,
        human_gate_receipt,
        scaffold_preset,
        intent.rules_store,
        [s.step_id for s in steps],
    )

    return OnboardingPlan(
        plan_id=plan_id,
        root_fingerprint=fingerprint,
        canonical_root=facts.canonical_root,
        intent=intent,
        human_gate_receipt=human_gate_receipt,
        scaffold_preset=scaffold_preset,
        rules_store=intent.rules_store,
        ordered_steps=steps,
        warnings=tuple(warnings),
        requires_confirmation=True,
    )


# The exact closed key set a plan record may carry. An unknown or missing key is
# a malformed plan (Redmine #13501 j#74844).
_PLAN_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "plan_id",
        "root_fingerprint",
        "canonical_root",
        "intent",
        "human_gate_receipt",
        "scaffold_preset",
        "rules_store",
        "ordered_steps",
        "warnings",
        "requires_confirmation",
    }
)


def rebuild_and_verify_plan(
    record: Mapping[str, object],
    fresh_facts: OnboardingFacts,
    *,
    gate_secret: str,
) -> OnboardingPlan:
    """Re-derive the authoritative plan and require the supplied record to equal
    it **exactly**, field-for-field (Redmine #13501 review F2 / j#74844).

    The caller-supplied ``preset`` / ``store`` / ``ordered_steps`` / ``warnings``
    / ``root`` are never trusted as authority: only the closed intent + human
    gate receipt are read to rebuild the canonical plan over the freshly
    re-inspected facts. Execution is permitted only when **every** human-visible
    authority-bound field of the supplied record — canonical root, fingerprint,
    intent, gate receipt, preset/store, the exact ordered steps *and summaries*,
    warnings, ``requires_confirmation``, and the keyed ``plan_id`` HMAC — matches
    the rebuilt canonical plan. Any unknown key, missing field, or mismatch is
    ``plan_unauthorized``: a caller cannot confirm one displayed plan and have a
    different one mutated, nor forge / recompute / drift a plan.
    """
    secret = require_gate_secret(gate_secret)
    if not isinstance(record, Mapping):
        raise PlanError("malformed_plan", "plan record must be a mapping")

    unknown = set(record) - _PLAN_RECORD_KEYS
    if unknown:
        raise PlanError(
            "plan_unauthorized",
            f"plan record carries unknown key(s): {sorted(map(str, unknown))}",
        )
    missing = _PLAN_RECORD_KEYS - set(record)
    if missing:
        raise PlanError(
            "plan_unauthorized",
            f"plan record is missing field(s): {sorted(missing)}",
        )

    raw_intent = record.get("intent")
    if not isinstance(raw_intent, Mapping):
        raise PlanError("malformed_plan", "plan record intent must be a mapping")
    try:
        intent = validate_onboarding_intent(raw_intent)
    except Exception as exc:  # noqa: BLE001 - surface as a plan error
        raise PlanError("malformed_plan", f"plan record intent is invalid: {exc}") from exc

    receipt = record.get("human_gate_receipt")
    receipt = receipt if isinstance(receipt, str) else None

    # Rebuild the authoritative plan from fresh facts + the closed intent. This
    # recomputes the HMAC over the *fresh* facts, so drift, tamper, forgery, and
    # a wrong secret all diverge from the canonical record.
    rebuilt = build_plan(
        fresh_facts, intent, human_gate_receipt=receipt, gate_secret=secret
    )

    # Exact, whole-record equality: every displayed authority-bound field must
    # equal the canonical rebuilt plan (constant-time on the plan_id).
    canonical = rebuilt.as_record()
    if not hmac.compare_digest(str(record.get("plan_id", "")), str(canonical["plan_id"])):
        raise PlanError(
            "plan_unauthorized",
            "plan_id does not match the authoritative plan rebuilt from the "
            "current facts and closed intent",
        )
    if dict(record) != canonical:
        raise PlanError(
            "plan_unauthorized",
            "a human-visible plan field does not match the authoritative rebuilt "
            "plan; refusing to apply a tampered / drifted plan",
        )
    return rebuilt
