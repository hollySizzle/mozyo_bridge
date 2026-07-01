"""OOP-first boundary for the ``handoff`` CLI command entries (Redmine #12936).

The ``handoff send`` / ``handoff reply`` / ``handoff ticketless-callback`` /
``handoff cross-workspace-consult`` command entries historically lived as four
thin procedural wrappers in ``application/commands.py``. Each carries only a
small amount of per-command *entry policy* on top of the shared
:func:`orchestrate_handoff` primitive:

- ``handoff send`` — apply the semantic target selection (Redmine #12663) before
  the unchanged identity gates, then orchestrate with no default kind.
- ``handoff reply`` — orchestrate with ``default_kind="reply"``.
- ``handoff ticketless-callback`` — orchestrate with ``default_kind="reply"`` and
  ``ticketless=True`` (Redmine #12703).
- ``handoff cross-workspace-consult`` — pin the receiver to ``codex`` and default
  ``--kind`` to ``design_consultation`` (Redmine #11779), then orchestrate with
  ``require_receiver_binding=True``.

This module carves those four entry bodies into an OOP-first boundary under
#12638 **without touching** :func:`orchestrate_handoff` itself, the handoff
implementation_request main-lane guard, the gateway route enforcement, or the
transport rail semantics (all explicitly out of #12936 scope):

- :class:`HandoffCommandOps` is the port for everything the use case needs from
  its environment, and :class:`LiveHandoffCommandOps` is the live adapter.
- :class:`HandoffCommandUseCase` holds the four entry bodies as ``run_*``
  methods.

The live adapter resolves each dependency *at call time* — ``orchestrate_handoff``
through the :mod:`commands` module and ``apply_handoff_selection`` through
:mod:`commands_target_select` via the same lazy import the original wrapper used —
so the existing CLI integration tests (which patch the low-level
``mozyo_bridge.application.commands.<fn>`` seams and run ``orchestrate_handoff``
for real) keep intercepting the side effects unchanged and no import cycle is
introduced (``commands`` imports this module only lazily inside the thin
wrappers). This is a pure, behavior-preserving restructuring: the CLI parser,
stdout, stderr, exit codes, marker/landing rail, and receiver-binding gate are
all identical to the original bodies.
"""

from __future__ import annotations

import argparse
from typing import Any, Protocol

CONSULT_DEFAULT_KIND = "design_consultation"
"""Default ``--kind`` for `handoff cross-workspace-consult` (Redmine #11779).

The cross-workspace primitive exists to carry design-consultation requests
through the target workspace's Codex gateway, so it defaults to
``design_consultation`` while still accepting any other ``KIND_LABELS`` value
(e.g. a cross-workspace ``review_request``) via an explicit ``--kind``.
"""


class HandoffCommandOps(Protocol):
    """Port: everything the handoff command use case needs from its environment.

    The use case depends only on this protocol, so it is exercisable with a
    synthetic fake. The live adapter routes each call through the real modules at
    call time so monkeypatched test doubles still intercept.
    """

    def apply_handoff_selection(self, args: argparse.Namespace) -> None: ...

    def orchestrate_handoff(
        self,
        args: argparse.Namespace,
        *,
        default_kind: str | None = None,
        require_receiver_binding: bool = False,
        ticketless: bool = False,
    ) -> int: ...


class LiveHandoffCommandOps:
    """Live :class:`HandoffCommandOps` over the real command helpers.

    ``orchestrate_handoff`` resolves *through the* :mod:`commands` *module at call
    time* and ``apply_handoff_selection`` through :mod:`commands_target_select`
    via the same lazy import the original wrapper used, so the ``handoff`` CLI
    integration tests that patch ``mozyo_bridge.application.commands.<fn>`` keep
    intercepting the side effects and no import cycle is introduced.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def apply_handoff_selection(self, args: argparse.Namespace) -> None:
        # Semantic target selection (Redmine #12663): `--select` resolves the
        # target `%pane` from role/session/repo/project before the unchanged
        # identity gates. Imported lazily from commands_target_select exactly as
        # the original wrapper did, so its patch seam is preserved.
        from mozyo_bridge.application.commands_target_select import (
            apply_handoff_selection,
        )

        apply_handoff_selection(args)

    def orchestrate_handoff(
        self,
        args: argparse.Namespace,
        *,
        default_kind: str | None = None,
        require_receiver_binding: bool = False,
        ticketless: bool = False,
    ) -> int:
        return self._commands().orchestrate_handoff(
            args,
            default_kind=default_kind,
            require_receiver_binding=require_receiver_binding,
            ticketless=ticketless,
        )


class HandoffCommandUseCase:
    """The four ``handoff`` command entry bodies behind the port.

    Each ``run_*`` method encodes one command's entry policy and delegates all
    actual gating to :func:`orchestrate_handoff`. Behavior is byte-for-byte
    identical to the original ``cmd_handoff_*`` wrappers.
    """

    def __init__(self, ops: HandoffCommandOps) -> None:
        self._ops = ops

    def run_send(self, args: argparse.Namespace) -> int:
        """`handoff send`: apply semantic target selection, then orchestrate."""
        self._ops.apply_handoff_selection(args)
        return self._ops.orchestrate_handoff(args)

    def run_reply(self, args: argparse.Namespace) -> int:
        """`handoff reply`: orchestrate with the reply default kind."""
        return self._ops.orchestrate_handoff(args, default_kind="reply")

    def run_ticketless_callback(self, args: argparse.Namespace) -> int:
        """`handoff ticketless-callback`: standard ticketless no-anchor callback.

        Returns a ticketless consultation hands-off result
        (``consultation_result`` / ``no_dispatch`` / ``blocked`` /
        ``anchor_required``) to the caller lane over the standard delivery rail
        (queue-enter / standard semantics, the same target admission /
        repo-identity / cross-session gates), WITHOUT a Redmine anchor and
        without fabricating one. The structured callback fields are carried as
        the workflow *result* (``DeliveryOutcome.ticketless_callback``), recorded
        distinctly from the transport outcome.

        It does NOT touch the Redmine-governed ``handoff reply`` / ``reply`` rail
        (those still require ``--issue`` + ``--journal``), and it fails closed if
        the dispatch decision is an actual child -> grandchild worker dispatch
        (which still requires a real anchor via ``handoff send``).
        """
        return self._ops.orchestrate_handoff(
            args, default_kind="reply", ticketless=True
        )

    def run_cross_workspace_consult(self, args: argparse.Namespace) -> int:
        """`handoff cross-workspace-consult`: cross-workspace design consult.

        A thin, boundary-preserving wrapper over :func:`orchestrate_handoff`. It
        encodes the *standard cross-workspace consult route* as a single command
        without re-implementing or relaxing any safety gate:

        - The receiver is fixed to ``codex``: the consult always lands on the
          target workspace's Codex gateway pane, never directly in a foreign
          Claude pane (a cross-session ``--to claude`` is blocked by the
          Cross-Workspace Handoff gate anyway). The target Codex reads the
          durable anchor and, if implementation is needed, performs the local
          same-session Claude handoff inside its own workspace.
        - ``--target`` and ``--target-repo`` are mandatory at the parser surface,
          so the cross-workspace identity gate (Redmine #10332 / #11301 / #11778)
          always runs. This *tightens* `handoff send` (which only runs the repo
          gate when ``--target-repo`` is supplied); it never relaxes it.
        - ``--kind`` defaults to ``design_consultation`` and may be overridden.
        - The durable source of truth stays the Redmine issue / Asana task; the
          pane notification is only the pointer.

        All actual gating (cross_session_claude block, target_repo identity gate,
        receiver-process binding, marker/landing rail, ``--target-repo auto``
        explicit-``%pane`` requirement) is delegated to
        :func:`orchestrate_handoff` so this wrapper cannot hide or weaken it.

        ``require_receiver_binding=True`` closes the boundary in **every** mode
        (Redmine #11779 review j#58685): the role-binding gate that `handoff send`
        runs only under ``queue-enter`` must also run under ``--mode standard`` /
        ``--mode pending`` here, or an explicit foreign-Claude ``%pane`` could be
        typed into under a ``to=codex`` marker — exactly the gateway bypass this
        primitive promises to prevent.
        """
        args.to = "codex"
        if getattr(args, "kind", None) is None:
            args.kind = CONSULT_DEFAULT_KIND
        return self._ops.orchestrate_handoff(args, require_receiver_binding=True)
