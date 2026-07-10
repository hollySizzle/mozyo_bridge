"""``onboarding`` CLI family registrar (Redmine #13498)."""

from __future__ import annotations

from .commands_onboarding import (
    cmd_onboarding_apply,
    cmd_onboarding_inspect,
    cmd_onboarding_plan,
    cmd_onboarding_resume,
)

__all__ = ("register",)


def register(sub) -> None:
    onboarding = sub.add_parser(
        "onboarding",
        help="Deterministic project onboarding (inspect/plan/apply/resume).",
    )
    onboarding_sub = onboarding.add_subparsers(
        dest="onboarding_command", required=True
    )

    inspect = onboarding_sub.add_parser(
        "inspect", help="Model-pre-launch preflight for a root (mutation: none)."
    )
    inspect.add_argument("--root", help="Root to inspect (default: cwd).")
    inspect.add_argument(
        "--ack",
        action="store_true",
        help="Issue a human caution acknowledgement receipt (requires the "
        "trusted gate secret) for a sync/cloud caution root.",
    )
    inspect.add_argument("--json", action="store_true", help="Emit JSON.")
    inspect.set_defaults(func=cmd_onboarding_inspect)

    plan = onboarding_sub.add_parser(
        "plan", help="Build a drift-bound plan from an OnboardingIntent (mutation: none)."
    )
    plan.add_argument("--root", help="Root to plan for (default: cwd).")
    plan.add_argument(
        "--intent",
        required=True,
        help="OnboardingIntent as an inline JSON string or a path to a JSON file.",
    )
    plan.add_argument(
        "--human-gate-receipt",
        dest="human_gate_receipt",
        help="Opaque caution acknowledgement receipt from `inspect --ack`.",
    )
    plan.add_argument("--json", action="store_true", help="Emit JSON.")
    plan.set_defaults(func=cmd_onboarding_plan)

    apply_p = onboarding_sub.add_parser(
        "apply", help="Apply a confirmed drift-bound plan (mutation: bounded)."
    )
    apply_p.add_argument(
        "--plan",
        required=True,
        help="Plan record as an inline JSON string or a path to a JSON file "
        "(from `onboarding plan`).",
    )
    apply_p.add_argument(
        "--confirm",
        action="store_true",
        help="Human confirmation of the visible mutation plan (required to apply).",
    )
    apply_p.add_argument("--json", action="store_true", help="Emit JSON.")
    apply_p.set_defaults(func=cmd_onboarding_apply)

    resume = onboarding_sub.add_parser(
        "resume", help="Resume an in-progress adoption from its receipt."
    )
    resume.add_argument("--root", help="Root to resume (default: cwd).")
    resume.add_argument("--json", action="store_true", help="Emit JSON.")
    resume.set_defaults(func=cmd_onboarding_resume)
