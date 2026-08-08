"""CLI surface for `handoff q-enter` (#12705 LLM-facing q-enter / submit-complete primitive).

A thin, additive front-door over the existing handoff rails. The LLM names a
high-level ``--intent`` (``worker_dispatch`` / ``reply`` / ``consultation_callback``)
and this command resolves — fail-closed — which product rail carries it and whether
a ticket anchor is required, then delegates the actual pane choreography (target
admission, repo/project/role identity gates, landing marker, Enter-only retry, C-u
rollback) to ``orchestrate_handoff`` unchanged. It is NOT a raw ``keys Enter``
alias and re-implements no safety gate.

The structured fields and most delivery/record knobs are reused verbatim from
``configure_handoff_parser`` (so a worker dispatch / reply carries its real ticket
anchor), with ``--source`` made optional because the ticketless
``consultation_callback`` intent rides the ``#12703 ticketless no-anchor callback
transport`` rail and never carries an anchor. Whether each anchor field is actually
required is decided per-intent in :func:`resolve_submit_plan`, so the LLM submits
one command and reads a structured result instead of hand-rolling the rail.

The handler lives here (not in ``application/commands.py``) so the front door adds
its own cohesive surface without growing the oversized command-handler module; it
imports ``orchestrate_handoff`` and delegates.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.commands import (
    die,
    orchestrate_handoff,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RECORD_FORMAT_BOTH,
    RECORD_FORMAT_JSON,
    RECORD_FORMAT_TEXT,
    RECORD_FORMATS,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
    RAIL_ANCHORED_REPLY,
    RAIL_ANCHORED_SEND,
    SUBMIT_INTENTS,
    SubmitOutcome,
    SubmitPlanError,
    derive_delivery_id,
    resolve_submit_plan,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    CALLBACK_REASONS,
    CLASSIFICATIONS,
    NEXT_ACTION_OWNERS,
    READ_CONTRACT_TOKENS,
    TICKETLESS_DISPATCH_DECISIONS,
)


def configure_q_enter_parser(parser_: argparse.ArgumentParser) -> None:
    """Configure `handoff q-enter` (#12705 LLM-facing q-enter / submit-complete primitive).

    Imported lazily inside :func:`register` (in ``cli_handoff``) to avoid an import
    cycle: ``configure_handoff_parser`` lives in ``cli_handoff`` and this module is
    imported by it.
    """
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff import (
        configure_handoff_parser,
    )

    parser_.add_argument(
        "--intent",
        required=True,
        choices=list(SUBMIT_INTENTS),
        help=(
            "High-level submit intent the front door resolves to a delivery rail "
            "and anchor requirement: `worker_dispatch` (anchored `handoff send`, "
            "ticket anchor required), `reply` (anchored `handoff reply`, ticket "
            "anchor required), or `consultation_callback` (the #12703 ticketless "
            "no-anchor callback rail, no anchor). You name the intent; the CLI owns "
            "rail selection, anchor fail-closed, retry, and rollback."
        ),
    )
    # Reuse the full anchored-send/reply surface (anchor flags, identity gates,
    # mode, retry/activation, record knobs). `--source` is optional here because
    # the ticketless intent carries none; `--kind` is optional because the front
    # door derives the default per intent. The per-intent requirement of each
    # anchor field is enforced in `resolve_submit_plan`, not by argparse.
    configure_handoff_parser(
        parser_,
        kind_required=False,
        source_required=False,
    )
    # Structured ticketless callback fields (#12703), optional here and required
    # only for `--intent consultation_callback`; the ticketless domain validates
    # them fail-closed. The `--dispatch-decision` choices expose ONLY the
    # no-anchor-safe decisions, so an actual worker dispatch is not expressible on
    # the ticketless rail.
    parser_.add_argument(
        "--classification",
        choices=list(CLASSIFICATIONS),
        help=(
            "consultation_callback result class: `consultation_result` / "
            "`no_dispatch` / `blocked` / `anchor_required`."
        ),
    )
    parser_.add_argument(
        "--dispatch-decision",
        dest="dispatch_decision",
        choices=list(TICKETLESS_DISPATCH_DECISIONS),
        help=(
            "consultation_callback hands-off decision (no-anchor-safe only): "
            "`no_dispatch`, `hand_back_to_caller`, "
            "`anchor_required_before_worker_dispatch`."
        ),
    )
    parser_.add_argument(
        "--workflow-next-owner",
        dest="workflow_next_owner",
        choices=list(NEXT_ACTION_OWNERS),
        help=(
            "consultation_callback workflow next-step owner (`caller` / `gateway` "
            "/ `worker` / `operator`), distinct from the transport next_action_owner."
        ),
    )
    parser_.add_argument(
        "--callback-reason",
        dest="callback_reason",
        choices=list(CALLBACK_REASONS),
        help="consultation_callback fixed reason token.",
    )
    parser_.add_argument(
        "--read-contract",
        dest="read_contract",
        choices=list(READ_CONTRACT_TOKENS),
        help=(
            "consultation_callback governing workflow-contract set "
            "(`grandparent_coordinator` / `project_gateway` / `coordinator`)."
        ),
    )
    parser_.add_argument(
        "--forward-action-id",
        dest="forward_action_id",
        default="",
        help=(
            "consultation_callback: echo the opaque forward generation id (Redmine #13583) the "
            "consultation carried, so a positively-delivered callback completes the exact forward "
            "generation. Omit for a plain callback."
        ),
    )


#: The exit code a front-door ``blocked`` terminal returns when the rail itself exited 0
#: (Redmine #14232 review j#95333 F2). Matches ``die``'s code so every non-delivered front-door
#: terminal — the plan fail-closes, a transport block, and an unconfirmed submission — is one
#: shell-visible failure class.
FRONT_DOOR_BLOCKED_EXIT_CODE = 1


def _emit_submit_outcome(outcome: SubmitOutcome, *, record_format: str) -> None:
    """Emit the front-door (workflow) result, separate from the transport outcome.

    Honors ``--record-format`` exactly like the transport ``_emit_outcome``: the
    pasteable record block first (``text`` / ``both``), then the single-line JSON
    last (``json`` / ``both``) so a script scraping the last JSON line of THIS
    envelope still works.

    Redmine #14232: on the dispatch path this envelope now comes **after** the transport
    outcome, because the front-door result is derived from it (``SubmitOutcome.from_transport``)
    rather than claimed before the rail ran. On the front door's own fail-closed path (a missing
    anchor / kind) this is still the only outcome and no pane was touched.
    """
    if record_format in (RECORD_FORMAT_TEXT, RECORD_FORMAT_BOTH):
        print("\n".join(outcome.record_lines()))
        if record_format == RECORD_FORMAT_BOTH:
            print("")
    if record_format in (RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH):
        print(outcome.to_json())


def cmd_handoff_q_enter(args: argparse.Namespace) -> int:
    """LLM-facing q-enter / submit-complete front door (#12705).

    Resolves the intent to a rail + anchor requirement (fail-closed), emits the
    structured front-door result, and — unless it fail-closed before touching a
    pane — delegates the delivery choreography to ``orchestrate_handoff``.
    """
    record_format = getattr(args, "record_format", None) or RECORD_FORMAT_BOTH
    if record_format not in RECORD_FORMATS:
        die(
            f"--record-format must be one of {sorted(RECORD_FORMATS)}; "
            f"got {record_format!r}"
        )

    intent = getattr(args, "intent", None)
    source = getattr(args, "source", None)
    issue = getattr(args, "issue", None)
    journal = getattr(args, "journal", None)
    task_id = getattr(args, "task_id", None)
    comment_id = getattr(args, "comment_id", None)
    anchor_url = getattr(args, "anchor_url", None)
    kind = getattr(args, "kind", None)
    receiver = getattr(args, "to", None)
    classification = getattr(args, "classification", None)

    # Deterministic idempotency id over the logical payload identity (not the
    # resolved pane / attempt), so a duplicate submit is detectable. Computed once
    # and threaded onto args so the transport record's `- Submit:` line shows the
    # exact same id the front-door envelope printed.
    intent_label = intent if isinstance(intent, str) else str(intent)
    delivery_id = derive_delivery_id(
        intent=intent_label,
        receiver=receiver,
        source=source,
        issue=issue,
        journal=journal,
        task=task_id,
        kind=kind,
        classification=classification,
    )

    try:
        plan = resolve_submit_plan(
            intent,
            source=source,
            issue=bool(issue),
            journal=bool(journal),
            task=bool(task_id),
            comment=bool(comment_id),
            anchor_url=bool(anchor_url),
            kind=kind,
        )
    except SubmitPlanError as exc:
        _emit_submit_outcome(
            SubmitOutcome(
                intent=intent_label,
                resolved_rail=None,
                anchor_required=True,
                ticketless=False,
                delivery_id=delivery_id,
                dispatched=False,
                blocked=True,
                blocked_reason="anchor_required",
                guidance=str(exc),
            ),
            record_format=record_format,
        )
        return 1

    if plan.rail == RAIL_ANCHORED_SEND and not kind:
        # The anchored `handoff send` rail requires an explicit intent label; the
        # front door surfaces it as a structured next action rather than guessing.
        _emit_submit_outcome(
            SubmitOutcome(
                intent=plan.intent,
                resolved_rail=plan.rail,
                anchor_required=plan.anchor_required,
                ticketless=plan.ticketless,
                delivery_id=delivery_id,
                dispatched=False,
                blocked=True,
                blocked_reason="kind_required",
                guidance=(
                    "--intent worker_dispatch rides the anchored `handoff send` "
                    "rail and requires an explicit --kind <label> (e.g. "
                    "implementation_request / review_request)"
                ),
            ),
            record_format=record_format,
        )
        return 1

    # Thread the front-door telemetry so the transport delivery record carries the
    # composer-residue classification + the same delivery id.
    args.submit_intent = plan.intent
    args.submit_delivery_id = delivery_id

    # Redmine #14232 (j#84877 required correction 1): run the rail FIRST, then derive the
    # front-door result from its terminal outcome. This block used to emit
    # `SubmitOutcome(dispatched=True, blocked=False)` *above* the call, so a later `blocked` /
    # `turn_start_unconfirmed` left a "dispatched" front-door record standing — pre-transport
    # plan success masquerading as delivery success. The rail may also `die`
    # (`SystemExit`) on a blocked terminal; the front-door record is emitted on that path too
    # (in the `finally`) and the exit is then re-raised unchanged, so the CLI exit code and the
    # transport's own output stay exactly as before.
    #
    # Ordering consequence, deliberately accepted: the transport outcome is now printed before
    # the front-door envelope, so the LAST JSON line of the combined output is the front-door
    # one. Readers that scrape a delivery outcome match on its `status` + `reason` keys (see
    # `callback_send_port._parse_outcome`), which the front-door JSON does not carry, so a
    # reverse scan still finds the transport outcome.
    rc = 0
    try:
        rc = _run_resolved_rail(args, plan)
    finally:
        front_door = SubmitOutcome.from_transport(
            getattr(args, "delivery_outcome", None),
            plan_intent=plan.intent,
            rail=plan.rail,
            anchor_required=plan.anchor_required,
            ticketless=plan.ticketless,
            delivery_id=delivery_id,
        )
        _emit_submit_outcome(front_door, record_format=record_format)
    # Redmine #14232 review j#95333 F2: reconcile the process exit code with the front-door
    # classification. j#94407's acceptance names the exit code as one of the four surfaces that
    # must converge, and R1 converged the other three: a record that said `blocked` while the
    # shell reported success left a false success for every automated caller.
    #
    # Scope is deliberately the q-enter FRONT DOOR only. `handoff send`'s own rc for a
    # marker-unobserved `queue_enter` stays 0: #13262 reserved that rail contract ("a
    # queue-enter contract change would need its own design record"), and this issue does not
    # hold that authority. The front door is a different surface — #12705 built it so an LLM
    # reads ONE structured result — so converging it here is in scope and changes no rail.
    #
    # `pending_input` is not blocked (see `from_transport`), so an explicitly requested
    # `--mode pending` park still exits 0. A rail that already died keeps its own non-zero rc.
    if front_door.blocked and rc == 0:
        return FRONT_DOOR_BLOCKED_EXIT_CODE
    return rc


def _run_resolved_rail(args: argparse.Namespace, plan) -> int:
    """Drive the plan's resolved rail (extracted so the #14232 front-door derivation is one block).

    Byte-identical rail selection and the #13583 forward-generation completion hook; the only
    change is that it now returns to a caller which derives the front-door record from the
    transport outcome instead of having claimed one before this ran.
    """
    if plan.ticketless:
        rc = orchestrate_handoff(args, default_kind=plan.default_kind, ticketless=True)
        # Redmine #13583 R1-F1 / R2-F2: a consultation_callback that echoes a forward_action_id
        # completes the correlated forward generation — but ONLY on the transport's structured
        # positive delivery (`sent`/`ok`, marker observed). rc 0 also covers `pending_input` and a
        # marker-unobserved `queue_enter`, neither of which handed the message to the receiver.
        from mozyo_bridge.application.commands import delivery_was_positive
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (  # noqa: E501
            complete_forward_generation_on_callback,
        )

        complete_forward_generation_on_callback(args, delivered=delivery_was_positive(args))
        return rc
    if plan.rail == RAIL_ANCHORED_REPLY:
        return orchestrate_handoff(args, default_kind=plan.default_kind)
    return orchestrate_handoff(args)


def register_q_enter(handoff_sub) -> None:
    """Register the `handoff q-enter` subcommand onto the handoff subparsers.

    Owns the verbose help / description / epilog here (not in ``cli_handoff``) so
    the shared handoff registrar stays cohesive and under the module-health
    threshold, mirroring ``register_grandchild_realization`` /
    ``cli_handoff_ticketless``.
    """
    parser = handoff_sub.add_parser(
        "q-enter",
        help=(
            "LLM-facing q-enter / submit-complete front door — name a submit "
            "intent; the CLI picks the rail, owns the anchor requirement, retry, "
            "and rollback"
        ),
        description=(
            "Single LLM-facing submit primitive (#12705 LLM-facing q-enter / "
            "submit-complete primitive). #12698 GK3500 ticketless exploratory "
            "smoke surfaced that a receiver had to hand-roll the delivery rail — "
            "reasoning about whether `handoff reply` would fail closed without a "
            "Redmine anchor, whether to fall back to the low-level `message` "
            "transport, whether a read / landing marker was needed, whether a "
            "rollback happened, whether to retry, and whether raw `keys Enter` was "
            "allowed. Instead you name a high-level `--intent`:\n\n"
            "  worker_dispatch        anchored `handoff send` (ticket anchor "
            "required; the Redmine-governed worker-dispatch anchor requirement is "
            "not relaxed)\n"
            "  reply                  anchored `handoff reply` (ticket anchor "
            "required)\n"
            "  consultation_callback  the #12703 ticketless no-anchor callback "
            "rail (no anchor; structured callback fields)\n\n"
            "The CLI resolves the rail + anchor requirement fail-closed (a missing "
            "anchor on an anchored intent returns a structured next action, not a "
            "guessed delivery), derives a deterministic delivery id for duplicate "
            "prevention, classifies the post-delivery composer residue "
            "(`not_typed` / `typed_but_pending` / `cleared` / "
            "`unsafe_state_requires_fresh_receiver`), and delegates the actual "
            "target admission / repo / project / role identity gates, landing "
            "marker, Enter-only retry, and C-u rollback to the standard rail "
            "unchanged. It is NOT a raw `keys Enter` alias. The front-door "
            "(workflow) result is emitted distinctly from the transport outcome "
            "(status / reason / marker)."
        ),
        epilog=(
            "Examples:\n"
            "  # Return a no-anchor consultation result (no ticket anchor):\n"
            "  mozyo-bridge handoff q-enter --intent consultation_callback \\\n"
            "    --to codex --target %0 --target-repo auto \\\n"
            "    --classification no_dispatch \\\n"
            "    --dispatch-decision hand_back_to_caller \\\n"
            "    --workflow-next-owner caller \\\n"
            "    --callback-reason no_dispatch_decided \\\n"
            "    --read-contract grandparent_coordinator\n\n"
            "  # Dispatch an anchored worker request (anchor required):\n"
            "  mozyo-bridge handoff q-enter --intent worker_dispatch \\\n"
            "    --to claude --target %1 --source redmine \\\n"
            "    --issue 12705 --journal 67162 --kind implementation_request"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configure_q_enter_parser(parser)
    parser.set_defaults(func=cmd_handoff_q_enter)
