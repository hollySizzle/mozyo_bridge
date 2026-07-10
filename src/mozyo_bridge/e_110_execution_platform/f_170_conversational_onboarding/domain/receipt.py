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

    def completed(self) -> "OnboardingReceipt":
        """Return a copy marked ``complete`` (all steps settled precondition)."""
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


def serialize_receipt(receipt: OnboardingReceipt) -> str:
    """Serialize to canonical, deterministic JSON (sorted keys), no secrets."""
    record = {
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
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_receipt(text: str) -> OnboardingReceipt:
    """Parse receipt JSON, failing closed with :class:`ReceiptError`.

    A malformed / non-object / wrong-version / unknown-key / bad-status receipt
    raises — the preflight then classifies the root as ``blocked`` (a broken
    receipt is a hard block, never silently treated as absent).
    """
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
    if not isinstance(scaffold_preset, str) or not scaffold_preset:
        raise ReceiptError("onboarding receipt is missing scaffold_preset")
    if not isinstance(rules_store, str) or not rules_store:
        raise ReceiptError("onboarding receipt is missing rules_store")

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

    return OnboardingReceipt(
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
