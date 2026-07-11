"""Bare-``mozyo`` onboarding entry hook + driver (Redmine #13497).

The dispatch the bare-``mozyo`` entrypoint delegates to when there is no explicit
subcommand. It classifies the current root and routes:

- **valid complete adoption** (typed config / valid complete signed receipt /
  anchor, mount-independently — j#74919 R1) → the existing backend-aware launch,
  byte/behaviour-invariant; the fresh-adoption mount/path classifier is bypassed
  so an inconclusive mount never blocks a working adopted launch;
- **broken adoption** (unreadable config / unverifiable receipt) → fail-closed
  render, never launched or adopted over;
- **adoption in progress** (valid signed receipt) → re-verify + drive
  ``resume_onboarding`` one idempotent step at a time after a model-external human
  confirmation (j#74919 R3) — no explicit subcommand required of the human;
- otherwise a **fresh root** → the full mount-safe :func:`inspect_onboarding`,
  then: hard block → render reason; sync/cloud caution → CLI-held human ack;
  unadopted → the provider-neutral conversation → visible plan → model-external
  confirmation → #13498 ``apply_plan``.

One bare ``mozyo`` must reach both project adoption **and** the herdr slot launch
(#13497 User value; #13424 leaves backend launch to this US). So every leg that
reaches a *complete* signed receipt — the already-adopted entry, a fresh apply
that finishes, and a resume that finishes — ends by invoking the **same** existing
backend-aware ``launch_adopted`` callback exactly once (j#74933). A cancelled,
failed, in-progress, or broken outcome never launches. The launch is never
reimplemented here — reusing the callback preserves its ``--no-attach`` / ``--json``
/ ``--cc`` semantics.

The conversation model only ever produces a validated, closed ``OnboardingIntent``.
Mutation authority is exclusively #13498's ``build_plan`` / ``apply_plan`` /
``resume_onboarding``; raw provider output is never used as mutation input, and
the two human gates (caution ack, visible-plan confirmation) stay outside the
model. Nothing here persists a transcript, prompt, response, or credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Protocol

from ..domain.conversation_port import (
    ConversationContext,
    ConversationProvider,
    build_intent_schema,
    build_tool_schema,
    sanitize_facts,
)
from ..domain.plan import (
    PlanError,
    build_plan,
    compute_root_fingerprint,
    issue_human_gate_receipt,
    require_gate_secret,
)
from ..domain.preflight import (
    STATE_ADOPTED,
    STATE_ADOPTION_IN_PROGRESS,
    STATE_BLOCKED,
    STATE_CAUTION_REQUIRES_ACK,
)
from ..domain.receipt import RECEIPT_STATE_COMPLETE
from .adoption_probe import classify_adoption
from .apply_usecase import ApplyError, apply_plan, resume_onboarding
from .conversation_loop import Aborted, Cancelled, Ready, run_onboarding_conversation
from .inspect_usecase import inspect_onboarding

__all__ = (
    "HumanIO",
    "CliHumanIO",
    "run_bare_entry",
    "MAX_RESUME_STEPS",
)

#: A safety bound on the resume drive so a receipt that never settles cannot spin
#: forever; the spec's ordered steps are far fewer than this.
MAX_RESUME_STEPS = 16


class HumanIO(Protocol):
    """The model-external human surface: display, read a line, yes/no confirm."""

    def show(self, text: str) -> None: ...

    def prompt(self) -> str | None: ...

    def confirm(self, text: str) -> bool: ...


class CliHumanIO:
    """Default stdin/stdout human surface (no transcript is retained)."""

    def show(self, text: str) -> None:
        print(text)

    def prompt(self) -> str | None:
        try:
            return input("> ")
        except EOFError:
            return None

    def confirm(self, text: str) -> bool:
        try:
            reply = input(f"{text} [y/N] ")
        except EOFError:
            return False
        return reply.strip().lower() in ("y", "yes")


def run_bare_entry(
    *,
    target_root: Path,
    launch_adopted: Callable[[], int],
    provider: ConversationProvider,
    gate_secret: str | None,
    io: HumanIO | None = None,
    json_output: bool = False,
) -> int:
    """Route bare ``mozyo`` and drive onboarding. Returns a process exit code.

    ``target_root`` is the already-selected root (``--repo`` / ``MOZYO_REPO`` /
    adopted ancestor / canonical cwd — resolved by the CLI, Redmine #13497
    j#74936); every probe, plan, apply, and the launch operate on this same root.
    ``launch_adopted`` runs the existing backend-aware launch (passed in so this
    module never imports the CLI). ``provider`` is the conversation binding.

    ``json_output`` reflects the root ``--json`` machine-readable contract: an
    adopted-complete root still takes the existing (byte-compatible) JSON launch,
    but every route that would otherwise run *interactive* onboarding (prose +
    ``input()`` prompts) instead emits a single JSON status object and fails
    closed — never mixing prompts / prose into the JSON stream, reading stdin, or
    mutating (Redmine #13497 j#74970 F3).
    """
    io = io or CliHumanIO()

    status = classify_adoption(target_root, gate_secret=gate_secret)
    if status.is_complete:
        return launch_adopted()
    if json_output:
        return _bare_entry_json(status, target_root, gate_secret=gate_secret)
    if status.is_broken:
        io.show(f"mozyo: refusing to launch or onboard — {status.reason}.")
        io.show("Resolve the existing .mozyo-bridge state and retry.")
        return 1
    if status.is_in_progress:
        return _drive_resume(
            status.canonical_root,
            launch_adopted=launch_adopted, gate_secret=gate_secret, io=io,
        )

    # ABSENT — a fresh root. Apply the full mount-safe preflight before any offer.
    inspection = inspect_onboarding(target_root, gate_secret=gate_secret)
    preflight = inspection.preflight
    state = preflight.state

    if state == STATE_BLOCKED:
        io.show("mozyo: this directory cannot be adopted:")
        for reason in preflight.hard_block_reasons:
            io.show(f"  - {reason}")
        io.show("Run mozyo from a normal project directory (not your home root).")
        return 1
    if state == STATE_ADOPTED:  # defensive: a marker appeared between probes.
        return launch_adopted()
    if state == STATE_ADOPTION_IN_PROGRESS:  # defensive.
        return _drive_resume(
            Path(inspection.facts.canonical_root),
            launch_adopted=launch_adopted, gate_secret=gate_secret, io=io,
        )

    return _drive_fresh_onboarding(
        inspection, launch_adopted=launch_adopted,
        provider=provider, gate_secret=gate_secret, io=io,
    )


def _emit_json(record: dict, *, code: int) -> int:
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return code


def _bare_entry_json(status, target_root: Path, *, gate_secret) -> int:
    """Emit a single JSON status object for a non-adopted root under ``--json``.

    Interactive onboarding (a conversation and its ``input()`` confirmations)
    cannot run against a machine-readable, stdin-capturing ``--json`` invocation,
    so every non-adopted route fails closed with one JSON object and no prompt,
    stdin read, or mutation. ``inspect_onboarding`` is mutation-free, so probing
    a fresh root for its precise state is safe here.
    """
    if status.is_broken:
        return _emit_json(
            {"state": "blocked", "error": "onboarding_blocked",
             "reason": status.reason,
             "next_action": "resolve the existing .mozyo-bridge state"},
            code=1,
        )
    if status.is_in_progress:
        return _emit_json(
            {"state": STATE_ADOPTION_IN_PROGRESS,
             "error": "interactive_onboarding_required",
             "next_action": "run `mozyo` without --json to resume onboarding"},
            code=1,
        )

    inspection = inspect_onboarding(target_root, gate_secret=gate_secret)
    state = inspection.preflight.state
    if state == STATE_BLOCKED:
        return _emit_json(
            {"state": "blocked", "error": "onboarding_blocked",
             "reasons": list(inspection.preflight.hard_block_reasons)},
            code=1,
        )
    # unadopted / caution_requires_ack (and any defensive residue): a fresh
    # adoption needs the interactive conversation + human confirmations.
    return _emit_json(
        {"state": state, "error": "interactive_onboarding_required",
         "next_action":
             "run `mozyo` without --json to complete onboarding interactively"},
        code=1,
    )


def _drive_fresh_onboarding(
    inspection, *, launch_adopted, provider, gate_secret, io: HumanIO
) -> int:
    preflight = inspection.preflight
    facts = inspection.facts
    human_gate_receipt: str | None = None
    caution_reason: str | None = None

    # Sync/cloud caution acknowledgement — obtained from the human by the CLI,
    # never by the model, and bound to this root as an opaque receipt.
    if preflight.state == STATE_CAUTION_REQUIRES_ACK:
        caution_reason = facts.path_risk
        io.show("mozyo: this folder looks like a cloud-sync folder.")
        io.show("It will be adopted as a non-Git workspace and git init is refused.")
        if not io.confirm("Acknowledge and continue setting up here?"):
            io.show("mozyo: onboarding cancelled.")
            return 1
        try:
            secret = require_gate_secret(gate_secret)
        except PlanError as exc:
            io.show(f"mozyo: cannot proceed — {exc.message}")
            return 1
        human_gate_receipt = issue_human_gate_receipt(
            compute_root_fingerprint(facts), facts.path_risk, secret=secret
        )

    context = ConversationContext(
        facts=sanitize_facts(preflight, caution_reason=caution_reason),
        intent_schema=build_intent_schema(),
        tool_schema=build_tool_schema(),
    )
    io.show("mozyo: let's set up this project. Tell me what you're doing here.")
    first = io.prompt()
    if first is None:
        io.show("mozyo: onboarding cancelled.")
        return 1
    context = context.with_human(first)

    outcome = run_onboarding_conversation(provider, context, io)
    if isinstance(outcome, Cancelled):
        io.show("mozyo: onboarding cancelled.")
        return 1
    if isinstance(outcome, Aborted):
        io.show(f"mozyo: onboarding could not continue ({outcome.code}).")
        return 1
    assert isinstance(outcome, Ready)

    return _plan_confirm_apply(
        outcome.intent, launch_adopted=launch_adopted,
        human_gate_receipt=human_gate_receipt,
        gate_secret=gate_secret, cwd=Path(facts.canonical_root), io=io,
    )


def _plan_confirm_apply(
    intent, *, launch_adopted, human_gate_receipt, gate_secret, cwd: Path, io: HumanIO
) -> int:
    # Re-inspect: the plan binds to freshly re-probed facts, never model facts.
    inspection = inspect_onboarding(cwd, gate_secret=gate_secret)
    try:
        secret = require_gate_secret(gate_secret)
        plan = build_plan(
            inspection.facts, intent,
            human_gate_receipt=human_gate_receipt, gate_secret=secret,
        )
    except PlanError as exc:
        io.show(f"mozyo: cannot build a setup plan — {exc.message}")
        return 1

    # The visible-plan confirmation — model-external.
    io.show("")
    io.show("mozyo will apply this setup plan:")
    io.show(f"  preset: {plan.scaffold_preset}   rules store: {plan.rules_store}")
    for step in plan.ordered_steps:
        io.show(f"  - {step.summary}")
    for warning in plan.warnings:
        io.show(f"  ! {warning}")
    if not io.confirm("Apply this plan?"):
        io.show("mozyo: setup plan not applied.")
        return 1

    try:
        result = apply_plan(
            plan.as_record(), human_confirmed=True, gate_secret=secret
        )
    except ApplyError as exc:
        io.show(f"mozyo: setup failed — {exc.message}")
        if exc.next_action:
            io.show(f"  next: {exc.next_action}")
        return 1

    for step in result.applied_steps:
        io.show(f"  applied: {step}")
    if result.failed_step is not None:
        io.show(f"mozyo: setup stopped at {result.failed_step}.")
        if result.next_action:
            io.show(f"  next: {result.next_action}. Re-run `mozyo` to continue.")
        return 1
    if result.state != RECEIPT_STATE_COMPLETE:
        # Defensive: apply runs the whole bounded sequence, so success is
        # complete. Anything else is not a launchable adoption.
        io.show("mozyo: setup did not complete; re-run `mozyo` to continue.")
        return 1
    io.show("mozyo: project setup complete — launching.")
    return launch_adopted()


def _drive_resume(root: Path, *, launch_adopted, gate_secret, io: HumanIO) -> int:
    """Drive an in-progress adoption to completion, one idempotent step per call.

    The signed receipt + root identity are re-verified inside each
    ``resume_onboarding`` (via ``inspect_onboarding``); the human confirms once,
    model-externally, then the CLI drives the remaining steps. On failure it
    surfaces the receipt-derived next action and stops — re-runnable via bare
    ``mozyo`` (j#74919 R3). No destructive rollback or gate override.
    """
    io.show("mozyo: an in-progress project setup was found here.")
    if not io.confirm("Resume it now?"):
        io.show("mozyo: resume cancelled. Re-run `mozyo` to continue later.")
        return 1

    for _ in range(MAX_RESUME_STEPS):
        try:
            result = resume_onboarding(root, gate_secret=gate_secret)
        except ApplyError as exc:
            io.show(f"mozyo: resume failed — {exc.message}")
            if exc.next_action:
                io.show(f"  next: {exc.next_action}")
            return 1
        if result.state == RECEIPT_STATE_COMPLETE:
            io.show("mozyo: project setup complete — launching.")
            return launch_adopted()
        if result.failed_step is not None:
            io.show(f"mozyo: resume stopped at {result.failed_step}.")
            if result.next_action:
                io.show(f"  next: {result.next_action}. Re-run `mozyo` to continue.")
            return 1
        for step in result.applied_steps:
            io.show(f"  applied: {step}")

    io.show("mozyo: resume did not complete; re-run `mozyo` to continue.")
    return 1
