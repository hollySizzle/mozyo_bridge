"""``onboarding`` CLI command handlers (Redmine #13498).

Exposes the deterministic onboarding tools as CLI subcommands — ``inspect`` /
``plan`` / ``apply`` / ``resume`` — plus a small ``maybe_resume_bare_mozyo``
hook the bare-``mozyo`` entrypoint calls to reroute an ``adoption_in_progress``
root to resume instead of a normal launch.

The human confirmations that must live outside the model are CLI-held: the
caution acknowledgement is issued by ``inspect --ack`` (only from the trusted
environment's gate secret), and the visible-mutation-plan confirmation is the
explicit ``apply --confirm`` flag. The model never authors YAML, issues a gate
receipt, or self-confirms a plan.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..domain.intent import IntentError, validate_onboarding_intent
from ..domain.plan import (
    PlanError,
    build_plan,
    compute_root_fingerprint,
    issue_human_gate_receipt,
    parse_plan_record,
)
from ..domain.preflight import (
    STATE_ADOPTION_IN_PROGRESS,
    STATE_CAUTION_REQUIRES_ACK,
)
from .apply_usecase import ApplyError, apply_plan, resume_onboarding
from .inspect_usecase import inspect_onboarding

__all__ = (
    "GATE_SECRET_ENV",
    "cmd_onboarding_inspect",
    "cmd_onboarding_plan",
    "cmd_onboarding_apply",
    "cmd_onboarding_resume",
    "maybe_resume_bare_mozyo",
)

#: Trusted-environment secret keying the human-gate receipt HMAC. The model
#: never sees it, so it cannot forge or rebind a caution acknowledgement.
GATE_SECRET_ENV = "MOZYO_ONBOARDING_GATE_SECRET"


def _root_from_args(args: argparse.Namespace) -> Path:
    root = getattr(args, "root", None)
    return Path(root).expanduser() if root else Path.cwd()


def _gate_secret() -> str | None:
    secret = os.environ.get(GATE_SECRET_ENV)
    return secret or None


def _emit(record: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key, value in record.items():
        print(f"{key}: {value}")


def cmd_onboarding_inspect(args: argparse.Namespace) -> int:
    """``onboarding inspect`` — the model-pre-launch preflight (mutation: none).

    With ``--ack`` on a ``caution_requires_ack`` root it also issues the opaque
    human-gate receipt (requires the trusted gate secret) so the human's caution
    acknowledgement can be threaded into a subsequent ``plan``.
    """
    root = _root_from_args(args)
    inspection = inspect_onboarding(root)
    record = inspection.preflight.as_record()

    if getattr(args, "ack", False):
        if inspection.preflight.state != STATE_CAUTION_REQUIRES_ACK:
            record["ack"] = "not_required"
        else:
            secret = _gate_secret()
            if not secret:
                record["ack"] = "unavailable"
                record["ack_error"] = (
                    f"set {GATE_SECRET_ENV} in the trusted environment to issue "
                    "a caution acknowledgement receipt"
                )
            else:
                fp = compute_root_fingerprint(inspection.facts)
                record["human_gate_receipt"] = issue_human_gate_receipt(
                    fp, inspection.facts.path_risk, secret=secret
                )

    _emit(record, getattr(args, "json", False))
    return 0 if not inspection.preflight.is_hard_block else 1


def _load_intent_record(raw: str) -> dict:
    # Accept an inline JSON string or a path to a JSON file.
    candidate = Path(raw).expanduser()
    if candidate.exists():
        raw = candidate.read_text(encoding="utf-8")
    return json.loads(raw)


def cmd_onboarding_plan(args: argparse.Namespace) -> int:
    """``onboarding plan`` — re-inspect + build a drift-bound plan (mutation: none)."""
    root = _root_from_args(args)
    as_json = getattr(args, "json", False)
    try:
        intent_record = _load_intent_record(args.intent)
    except (OSError, ValueError) as exc:
        _emit({"error": "invalid_intent_json", "message": str(exc)}, as_json)
        return 2

    try:
        intent = validate_onboarding_intent(intent_record)
    except IntentError as exc:
        _emit(exc.as_record(), as_json)
        return 2

    inspection = inspect_onboarding(root)
    secret = _gate_secret() or ""
    try:
        plan = build_plan(
            inspection.facts,
            intent,
            human_gate_receipt=getattr(args, "human_gate_receipt", None),
            gate_secret=secret,
        )
    except PlanError as exc:
        _emit(exc.as_record(), as_json)
        return 2

    _emit(plan.as_record(), as_json)
    return 0


def cmd_onboarding_apply(args: argparse.Namespace) -> int:
    """``onboarding apply`` — apply a confirmed, drift-bound plan (mutation: bounded)."""
    as_json = getattr(args, "json", False)
    try:
        raw = args.plan
        candidate = Path(raw).expanduser()
        text = candidate.read_text(encoding="utf-8") if candidate.exists() else raw
        plan_record = json.loads(text)
    except (OSError, ValueError) as exc:
        _emit({"error": "invalid_plan_json", "message": str(exc)}, as_json)
        return 2

    try:
        plan = parse_plan_record(plan_record)
    except PlanError as exc:
        _emit(exc.as_record(), as_json)
        return 2

    try:
        result = apply_plan(plan, human_confirmed=bool(getattr(args, "confirm", False)))
    except ApplyError as exc:
        _emit(exc.as_record(), as_json)
        return 2

    _emit(result.as_record(), as_json)
    return 0 if result.failed_step is None else 1


def cmd_onboarding_resume(args: argparse.Namespace) -> int:
    """``onboarding resume`` — continue an in-progress adoption from its receipt."""
    as_json = getattr(args, "json", False)
    root = _root_from_args(args)
    try:
        result = resume_onboarding(root)
    except ApplyError as exc:
        _emit(exc.as_record(), as_json)
        return 2
    _emit(result.as_record(), as_json)
    return 0 if result.failed_step is None else 1


def maybe_resume_bare_mozyo(args: argparse.Namespace) -> int | None:
    """Reroute bare ``mozyo`` to resume when an adoption is in progress.

    Returns an exit code when it handled the invocation (the root is
    ``adoption_in_progress``, so a normal launch must not happen), or ``None`` to
    let the caller proceed with the normal backend-aware launch. Any failure to
    inspect is swallowed to ``None`` so this hook can never break a normal
    launch — the receipt-based reroute is a best-effort guard, and a genuinely
    broken root still fails closed in the launch adoption gate.
    """
    try:
        inspection = inspect_onboarding(Path.cwd())
    except Exception:  # noqa: BLE001 - never break launch on an inspect failure
        return None
    if inspection.preflight.state != STATE_ADOPTION_IN_PROGRESS:
        return None
    print(
        "onboarding is in progress at this root; resuming instead of launching "
        "(bare `mozyo` does not treat an in-progress adoption as a normal launch)."
    )
    try:
        result = resume_onboarding(Path.cwd())
    except ApplyError as exc:
        _emit(exc.as_record(), getattr(args, "json", False))
        return 2
    _emit(result.as_record(), getattr(args, "json", False))
    return 0 if result.failed_step is None else 1
