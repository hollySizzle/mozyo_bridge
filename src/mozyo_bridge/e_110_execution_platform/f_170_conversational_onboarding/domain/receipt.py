"""Credential-free onboarding receipt model (Redmine #13498).

The receipt is the durable, resumable record of an adoption in progress. It is
written atomically as the *first* apply step (recording ``adoption_in_progress``)
and updated to ``complete`` as the last, so a crash mid-apply leaves a receipt
that ``onboarding.resume`` can read to continue exactly where it stopped.

**The receipt is credential-free by construction.** It only ever stores step
identifiers, their status, the plan id, and content *fingerprints* (hashes) —
never a secret, token, config body, or conversation transcript. This module is
the pure model + (de)serialization; file IO lives in the application layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Mapping

__all__ = (
    "ONBOARDING_RECEIPT_VERSION",
    "RECEIPT_STATE_IN_PROGRESS",
    "RECEIPT_STATE_COMPLETE",
    "STEP_ONBOARDING_RECEIPT",
    "STEP_SCAFFOLD_APPLY",
    "STEP_CONFIG_WRITE_ONCE",
    "STEP_RULES_INSTALL",
    "STEP_WORKSPACE_REGISTER",
    "STEP_VERIFY",
    "STEP_FINALIZE",
    "ORDERED_STEPS",
    "STEP_STATUS_PENDING",
    "STEP_STATUS_DONE",
    "STEP_STATUS_NO_OP",
    "STEP_STATUS_FAILED",
    "OnboardingReceipt",
    "ReceiptError",
    "parse_receipt",
    "serialize_receipt",
)

ONBOARDING_RECEIPT_VERSION = 1

RECEIPT_STATE_IN_PROGRESS = "adoption_in_progress"
RECEIPT_STATE_COMPLETE = "complete"
_RECEIPT_STATES: frozenset[str] = frozenset(
    {RECEIPT_STATE_IN_PROGRESS, RECEIPT_STATE_COMPLETE}
)

# Ordered idempotent apply steps (spec "ordered steps"). ``STEP_ONBOARDING_RECEIPT``
# is the receipt write itself, so it is always ``done`` once a receipt exists.
#
# NOTE — ordering correction vs the design spec (raised for #13501 design-
# compliance mid-review): the spec numbers ``scaffold apply`` (2) before
# ``rules install`` (4), but ``scaffold.rules.write_scaffold`` calls
# ``require_installed_preset`` and ``die()``s when the chosen store has no
# installed preset. The dependency is mechanical — scaffold rendering cannot run
# before the rules it renders from are installed — so this runner installs rules
# *before* scaffold apply. The step identities are unchanged; only the execution
# order is corrected to satisfy the hard dependency.
STEP_ONBOARDING_RECEIPT = "onboarding_receipt"
STEP_RULES_INSTALL = "rules_install"
STEP_SCAFFOLD_APPLY = "scaffold_apply"
STEP_CONFIG_WRITE_ONCE = "config_write_once"
STEP_WORKSPACE_REGISTER = "workspace_register"
STEP_VERIFY = "verify"
STEP_FINALIZE = "finalize"
ORDERED_STEPS: tuple[str, ...] = (
    STEP_ONBOARDING_RECEIPT,
    STEP_RULES_INSTALL,
    STEP_SCAFFOLD_APPLY,
    STEP_CONFIG_WRITE_ONCE,
    STEP_WORKSPACE_REGISTER,
    STEP_VERIFY,
    STEP_FINALIZE,
)

STEP_STATUS_PENDING = "pending"
STEP_STATUS_DONE = "done"
STEP_STATUS_NO_OP = "no_op"
STEP_STATUS_FAILED = "failed"
_STEP_STATUSES: frozenset[str] = frozenset(
    {STEP_STATUS_PENDING, STEP_STATUS_DONE, STEP_STATUS_NO_OP, STEP_STATUS_FAILED}
)
# Statuses that count as "this step no longer needs to run" for resume.
_SETTLED_STATUSES: frozenset[str] = frozenset({STEP_STATUS_DONE, STEP_STATUS_NO_OP})

# Closed vocabularies the receipt's plan parameters must belong to (Redmine
# #13501 review F3). Kept as a literal set here to avoid a circular import with
# ``plan`` (which imports this module); it mirrors the values of
# ``plan.PRESET_INTENT_TO_SCAFFOLD`` and ``intent.INTENT_RULES_STORES``.
_VALID_SCAFFOLD_PRESETS: frozenset[str] = frozenset(
    {"none", "asana", "redmine", "redmine-governed", "redmine-rails", "redmine-rails-governed"}
)
_VALID_RULES_STORES: frozenset[str] = frozenset({"central", "repo_local"})


class ReceiptError(ValueError):
    """A receipt on disk is unreadable or does not match the closed schema."""


@dataclass(frozen=True)
class OnboardingReceipt:
    """The closed, credential-free onboarding receipt record.

    ``scaffold_preset`` / ``rules_store`` are the *plan parameters* resume needs
    to execute the remaining idempotent steps without the original plan object
    (``onboarding.resume`` input is only the current root). They are declarative
    selections, never secrets.
    """

    root_fingerprint: str
    plan_id: str
    scaffold_preset: str
    rules_store: str
    state: str = RECEIPT_STATE_IN_PROGRESS
    step_status: Mapping[str, str] = field(default_factory=dict)
    failed_step: str | None = None
    failed_reason: str | None = None
    version: int = ONBOARDING_RECEIPT_VERSION

    def status_of(self, step: str) -> str:
        return self.step_status.get(step, STEP_STATUS_PENDING)

    def is_settled(self, step: str) -> bool:
        """True when ``step`` has completed (``done``/``no_op``) and needn't rerun."""
        return self.status_of(step) in _SETTLED_STATUSES

    def next_pending_step(self) -> str | None:
        """The first ordered step not yet settled, or ``None`` when all are done."""
        for step in ORDERED_STEPS:
            if not self.is_settled(step):
                return step
        return None

    def with_step(
        self, step: str, status: str, *, reason: str | None = None
    ) -> "OnboardingReceipt":
        """Return a copy recording ``step`` at ``status`` (and optional failure)."""
        if step not in ORDERED_STEPS:
            raise ReceiptError(f"unknown onboarding step {step!r}")
        if status not in _STEP_STATUSES:
            raise ReceiptError(f"unknown step status {status!r}")
        updated = dict(self.step_status)
        updated[step] = status
        failed_step = self.failed_step
        failed_reason = self.failed_reason
        if status == STEP_STATUS_FAILED:
            failed_step, failed_reason = step, reason
        elif step == failed_step:
            # A previously failed step that now settled clears the failure.
            failed_step, failed_reason = None, None
        return OnboardingReceipt(
            root_fingerprint=self.root_fingerprint,
            plan_id=self.plan_id,
            scaffold_preset=self.scaffold_preset,
            rules_store=self.rules_store,
            state=self.state,
            step_status=updated,
            failed_step=failed_step,
            failed_reason=failed_reason,
            version=self.version,
        )

    def all_settled(self) -> bool:
        return all(self.is_settled(step) for step in ORDERED_STEPS)

    def completed(self) -> "OnboardingReceipt":
        """Return a copy marked ``complete`` — every step must be settled first.

        Enforcing the precondition (Redmine #13501 review F3) makes ``complete``
        mean exactly "all ordered steps done/no-op"; a receipt can never claim
        completion with pending or failed steps.
        """
        if not self.all_settled():
            raise ReceiptError(
                "cannot mark onboarding receipt complete: not all steps are settled"
            )
        return OnboardingReceipt(
            root_fingerprint=self.root_fingerprint,
            plan_id=self.plan_id,
            scaffold_preset=self.scaffold_preset,
            rules_store=self.rules_store,
            state=RECEIPT_STATE_COMPLETE,
            step_status=dict(self.step_status),
            failed_step=None,
            failed_reason=None,
            version=self.version,
        )


def _receipt_body(receipt: OnboardingReceipt) -> dict[str, object]:
    return {
        "version": receipt.version,
        "root_fingerprint": receipt.root_fingerprint,
        "plan_id": receipt.plan_id,
        "scaffold_preset": receipt.scaffold_preset,
        "rules_store": receipt.rules_store,
        "state": receipt.state,
        "step_status": dict(sorted(receipt.step_status.items())),
        "failed_step": receipt.failed_step,
        "failed_reason": receipt.failed_reason,
    }


def _receipt_signature(body: Mapping[str, object], secret: str) -> str:
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def serialize_receipt(receipt: OnboardingReceipt, *, secret: str) -> str:
    """Serialize to canonical JSON with a trusted-secret HMAC signature.

    The signature (Redmine #13501 review F3) makes the receipt tamper-evident:
    a hand-forged receipt has no valid signature, so :func:`parse_receipt`
    rejects it and the preflight classifies the root as ``blocked``. Requires a
    non-empty secret. The body carries no secrets — only step status, plan
    parameters, and fingerprints.
    """
    if not isinstance(secret, str) or not secret:
        raise ReceiptError("a non-empty trusted secret is required to sign the receipt")
    body = _receipt_body(receipt)
    record = dict(body)
    record["signature"] = _receipt_signature(body, secret)
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_receipt(text: str, *, secret: str) -> OnboardingReceipt:
    """Parse + fully validate a receipt, failing closed with :class:`ReceiptError`.

    Beyond structural checks, this enforces (Redmine #13501 review F3): closed
    ``scaffold_preset`` / ``rules_store`` vocabularies; a valid trusted-secret
    signature; ``complete`` iff every step is settled; and coherence between
    ``failed_step`` and the step statuses. Any violation raises — the preflight
    then classifies the root as ``blocked`` (a broken / forged receipt is a hard
    block, never silently treated as absent or trusted for mutation).
    """
    if not isinstance(secret, str) or not secret:
        raise ReceiptError("a non-empty trusted secret is required to verify the receipt")
    try:
        record = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ReceiptError(f"onboarding receipt is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ReceiptError("onboarding receipt must be a JSON object")

    allowed = {
        "version",
        "root_fingerprint",
        "plan_id",
        "scaffold_preset",
        "rules_store",
        "state",
        "step_status",
        "failed_step",
        "failed_reason",
        "signature",
    }
    unknown = set(record) - allowed
    if unknown:
        raise ReceiptError(f"onboarding receipt has unknown key(s): {sorted(unknown)}")

    version = record.get("version", ONBOARDING_RECEIPT_VERSION)
    if version != ONBOARDING_RECEIPT_VERSION:
        raise ReceiptError(f"unsupported onboarding receipt version {version!r}")

    state = record.get("state")
    if state not in _RECEIPT_STATES:
        raise ReceiptError(f"onboarding receipt has unknown state {state!r}")

    fingerprint = record.get("root_fingerprint")
    plan_id = record.get("plan_id")
    scaffold_preset = record.get("scaffold_preset")
    rules_store = record.get("rules_store")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ReceiptError("onboarding receipt is missing root_fingerprint")
    if not isinstance(plan_id, str) or not plan_id:
        raise ReceiptError("onboarding receipt is missing plan_id")
    if scaffold_preset not in _VALID_SCAFFOLD_PRESETS:
        raise ReceiptError(
            f"onboarding receipt scaffold_preset {scaffold_preset!r} is not a "
            "recognised scaffold preset"
        )
    if rules_store not in _VALID_RULES_STORES:
        raise ReceiptError(
            f"onboarding receipt rules_store {rules_store!r} is not a recognised store"
        )

    raw_status = record.get("step_status", {})
    if not isinstance(raw_status, dict):
        raise ReceiptError("onboarding receipt step_status must be an object")
    step_status: dict[str, str] = {}
    for step, status in raw_status.items():
        if step not in ORDERED_STEPS:
            raise ReceiptError(f"onboarding receipt references unknown step {step!r}")
        if status not in _STEP_STATUSES:
            raise ReceiptError(
                f"onboarding receipt step {step!r} has unknown status {status!r}"
            )
        step_status[step] = status

    failed_step = record.get("failed_step")
    if failed_step is not None and failed_step not in ORDERED_STEPS:
        raise ReceiptError(f"onboarding receipt failed_step {failed_step!r} is unknown")
    failed_reason = record.get("failed_reason")
    if failed_reason is not None and not isinstance(failed_reason, str):
        raise ReceiptError("onboarding receipt failed_reason must be a string or null")

    receipt = OnboardingReceipt(
        root_fingerprint=fingerprint,
        plan_id=plan_id,
        scaffold_preset=scaffold_preset,
        rules_store=rules_store,
        state=state,
        step_status=step_status,
        failed_step=failed_step,
        failed_reason=failed_reason,
        version=ONBOARDING_RECEIPT_VERSION,
    )

    # Signature verification (tamper-evidence).
    signature = record.get("signature")
    expected = _receipt_signature(_receipt_body(receipt), secret)
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise ReceiptError("onboarding receipt signature is missing or invalid")

    # Coherence: failed_step iff exactly that step is `failed`.
    failed_steps = {s for s, st in step_status.items() if st == STEP_STATUS_FAILED}
    if failed_step is not None:
        if step_status.get(failed_step) != STEP_STATUS_FAILED:
            raise ReceiptError(
                f"onboarding receipt failed_step {failed_step!r} is not marked failed"
            )
    if failed_steps and failed_step not in failed_steps:
        raise ReceiptError("onboarding receipt has a failed step but no matching failed_step")
    if len(failed_steps) > 1:
        raise ReceiptError("onboarding receipt has more than one failed step")

    # Coherence: `complete` iff every step is settled and nothing failed.
    if state == RECEIPT_STATE_COMPLETE:
        if failed_step is not None or failed_steps:
            raise ReceiptError("onboarding receipt is complete but has a failed step")
        if not receipt.all_settled():
            raise ReceiptError(
                "onboarding receipt is complete but not all steps are settled"
            )

    return receipt
