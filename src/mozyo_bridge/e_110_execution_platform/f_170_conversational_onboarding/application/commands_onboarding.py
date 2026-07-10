"""``onboarding`` CLI command handlers (Redmine #13498 / #13501).

Exposes the deterministic onboarding tools — ``inspect`` / ``plan`` / ``apply``
/ ``resume``. The human confirmations that must live outside the model are
CLI-held: the caution acknowledgement is issued by ``inspect --ack`` (only from
the trusted-environment gate secret), and the visible-mutation-plan confirmation
is the explicit ``apply --confirm`` flag. The model never authors YAML, issues a
gate receipt, or self-confirms a plan.

The bare-``mozyo`` entry hook is deliberately **not** here — the conversation
provider / bare entry surface is owned by #13497 (Start Gate #13498 j#74722).
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
    require_gate_secret,
)
from ..domain.preflight import STATE_CAUTION_REQUIRES_ACK
from .apply_usecase import ApplyError, apply_plan, resume_onboarding
from .inspect_usecase import inspect_onboarding

__all__ = (
    "GATE_SECRET_ENV",
    "cmd_onboarding_inspect",
    "cmd_onboarding_plan",
    "cmd_onboarding_apply",
    "cmd_onboarding_resume",
)

#: Trusted-environment secret keying the human-gate receipt + plan authority. The
#: model never sees it, so it cannot forge a receipt / plan (Redmine #13501 F1).
GATE_SECRET_ENV = "MOZYO_ONBOARDING_GATE_SECRET"


def _root_from_args(args: argparse.Namespace) -> Path:
    root = getattr(args, "root", None)
    return Path(root).expanduser() if root else Path.cwd()


def _gate_secret() -> str | None:
    return os.environ.get(GATE_SECRET_ENV)


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
    secret = _gate_secret()
    inspection = inspect_onboarding(root, gate_secret=secret)
    record = inspection.preflight.as_record()

    if getattr(args, "ack", False):
        if inspection.preflight.state != STATE_CAUTION_REQUIRES_ACK:
            record["ack"] = "not_required"
        else:
            try:
                secret_ok = require_gate_secret(secret)
            except PlanError as exc:
                record["ack"] = "unavailable"
                record["ack_error"] = exc.message
            else:
                fp = compute_root_fingerprint(inspection.facts)
                record["human_gate_receipt"] = issue_human_gate_receipt(
                    fp, inspection.facts.path_risk, secret=secret_ok
                )

    _emit(record, getattr(args, "json", False))
    return 0 if not inspection.preflight.is_hard_block else 1


def _load_json_arg(raw: str) -> object:
    # Accept an inline JSON string or a path to a JSON file.
    candidate = Path(raw).expanduser()
    if candidate.exists():
        raw = candidate.read_text(encoding="utf-8")
    return json.loads(raw)


def cmd_onboarding_plan(args: argparse.Namespace) -> int:
    """``onboarding plan`` — re-inspect + build an authority-bound plan (mutation: none)."""
    root = _root_from_args(args)
    as_json = getattr(args, "json", False)
    secret = _gate_secret()
    try:
        intent_record = _load_json_arg(args.intent)
    except (OSError, ValueError) as exc:
        _emit({"error": "invalid_intent_json", "message": str(exc)}, as_json)
        return 2
    if not isinstance(intent_record, dict):
        _emit({"error": "invalid_intent_json", "message": "intent must be an object"}, as_json)
        return 2

    try:
        intent = validate_onboarding_intent(intent_record)
    except IntentError as exc:
        _emit(exc.as_record(), as_json)
        return 2

    inspection = inspect_onboarding(root, gate_secret=secret)
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
    """``onboarding apply`` — apply a confirmed, authority-bound plan (mutation: bounded)."""
    as_json = getattr(args, "json", False)
    secret = _gate_secret()
    try:
        plan_record = _load_json_arg(args.plan)
    except (OSError, ValueError) as exc:
        _emit({"error": "invalid_plan_json", "message": str(exc)}, as_json)
        return 2
    if not isinstance(plan_record, dict):
        _emit({"error": "invalid_plan_json", "message": "plan must be an object"}, as_json)
        return 2

    try:
        result = apply_plan(
            plan_record,
            human_confirmed=bool(getattr(args, "confirm", False)),
            gate_secret=secret,
        )
    except ApplyError as exc:
        _emit(exc.as_record(), as_json)
        return 2

    _emit(result.as_record(), as_json)
    return 0 if result.failed_step is None else 1


def cmd_onboarding_resume(args: argparse.Namespace) -> int:
    """``onboarding resume`` — perform one pending step of an in-progress adoption."""
    as_json = getattr(args, "json", False)
    root = _root_from_args(args)
    secret = _gate_secret()
    try:
        result = resume_onboarding(root, gate_secret=secret)
    except ApplyError as exc:
        _emit(exc.as_record(), as_json)
        return 2
    _emit(result.as_record(), as_json)
    return 0 if result.failed_step is None else 1
