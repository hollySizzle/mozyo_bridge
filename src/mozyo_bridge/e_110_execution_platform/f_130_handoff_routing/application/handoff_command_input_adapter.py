"""Namespace -> :class:`HandoffCommandInput` adapter (Redmine #13729, tranche 1).

:class:`HandoffNamespaceAdapter` is the single boundary where an
``argparse.Namespace`` (plus ``orchestrate_handoff``'s entry-policy keyword
parameters) is converted into the typed
:class:`~mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input.HandoffCommandInput`
value object. "The Namespace ends here" (design j#78394): later tranches take the
value object, not the Namespace.

The conversion is a *dumb field capture*: every field is read with exactly the
``getattr(args, "<name>", <default>)`` default the original ``orchestrate_handoff``
body used at its primary read site, and no coercion is applied. The body keeps
its own normalization (``inp.mode or MODE_QUEUE_ENTER``,
``float(inp.landing_timeout or 8.0)``, ``int(inp.read_lines or 50)``, ...), so
substituting ``inp.<field>`` for the ``getattr`` call is byte-for-byte
behaviour-preserving.

``mode`` and ``landing_timeout`` are the two fields whose original body read used
a non-``None`` / dual default. Both are captured raw with a ``None`` default: the
body's surviving ``or MODE_QUEUE_ENTER`` / ``or 8.0`` reproduce the original value
for every input (absent attribute, ``None``, ``""``, ``0``), and
``landing_timeout``'s second read site (``getattr(args, "landing_timeout", None)``)
already used a ``None`` default.
"""

from __future__ import annotations

import argparse

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (
    HandoffCommandInput,
)


class HandoffNamespaceAdapter:
    """Convert an ``argparse.Namespace`` into a :class:`HandoffCommandInput`."""

    @staticmethod
    def from_namespace(
        args: argparse.Namespace,
        *,
        default_kind: str | None = None,
        require_receiver_binding: bool = False,
        ticketless: bool = False,
        ticketless_consultation: bool = False,
        ticketless_work_intake: bool = False,
    ) -> HandoffCommandInput:
        """Snapshot ``args`` + entry policy into the frozen typed input.

        The keyword arguments carry ``orchestrate_handoff``'s per-command entry
        policy (formerly loose parameters); the ``getattr`` reads mirror the raw
        Namespace values with the original per-site defaults.
        """
        return HandoffCommandInput(
            # entry policy (from the caller's keyword parameters)
            default_kind=default_kind,
            require_receiver_binding=require_receiver_binding,
            ticketless=ticketless,
            ticketless_consultation=ticketless_consultation,
            ticketless_work_intake=ticketless_work_intake,
            # routing / receiver
            to=getattr(args, "to", None),
            source=getattr(args, "source", None),
            kind=getattr(args, "kind", None),
            mode=getattr(args, "mode", None),
            force=getattr(args, "force", False),
            summary=getattr(args, "summary", None),
            # anchor
            task_id=getattr(args, "task_id", None),
            comment_id=getattr(args, "comment_id", None),
            anchor_url=getattr(args, "anchor_url", None),
            issue=getattr(args, "issue", None),
            journal=getattr(args, "journal", None),
            # ticketless payloads
            work_shape=getattr(args, "work_shape", None),
            consultation_kind=getattr(args, "consultation_kind", None),
            classification=getattr(args, "classification", None),
            dispatch_decision=getattr(args, "dispatch_decision", None),
            workflow_next_owner=getattr(args, "workflow_next_owner", None),
            callback_reason=getattr(args, "callback_reason", None),
            callback_to_role=getattr(args, "callback_to_role", None),
            callback_methods=getattr(args, "callback_methods", None),
            read_contract=getattr(args, "read_contract", None),
            forward_action_id=getattr(args, "forward_action_id", ""),
            # target / activation
            target=getattr(args, "target", None),
            target_project=getattr(args, "target_project", None),
            no_target_activation=getattr(args, "no_target_activation", False),
            restore_previous_active=getattr(args, "restore_previous_active", False),
            # execution root / profile / contract
            workdir=getattr(args, "workdir", None),
            role_profile=getattr(args, "role_profile", None),
            profile_field=getattr(args, "profile_field", None),
            transition_role=getattr(args, "transition_role", None),
            workflow_contract=getattr(args, "workflow_contract", None),
            # transport rail knobs
            read_lines=getattr(args, "read_lines", 50),
            landing_timeout=getattr(args, "landing_timeout", None),
            submit_delay=getattr(args, "submit_delay", 0.2),
            queue_enter_retry_window=getattr(args, "queue_enter_retry_window", None),
            queue_enter_retry_interval=getattr(
                args, "queue_enter_retry_interval", None
            ),
        )
