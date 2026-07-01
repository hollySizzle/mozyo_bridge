"""OOP-first boundary for the ``notify-*`` CLI command wrappers (Redmine #12931).

The ``notify-*`` command family historically lived as two procedural bodies in
``application/commands.py``:

- ``notify_agent`` — the legacy queue path used by ``notify-*-legacy-task``. It
  drives the raw type-observe-marker-Enter TUI rail directly (a wrapper-only
  cleanup path that intentionally does *not* route through ``orchestrate_handoff``
  and therefore emits no structured durable record).
- ``_notify_standard_via_handoff`` — the standard adapter that maps the legacy
  Redmine-shaped ``notify-*`` flags onto ``orchestrate_handoff``'s normalized
  contract so ``notify-codex`` / ``notify-claude`` / ``notify-*-review*`` share a
  single orchestration rail with ``mozyo-bridge handoff`` / ``reply``, while
  preserving the legacy ``notified <agent>: journal=...`` success line.

This module carves both bodies into an OOP-first boundary under #12638 without
touching ``orchestrate_handoff`` itself, the handoff implementation_request
guard, or the gateway route enforcement surfaces (all out of #12931 scope):

- :class:`NotifyOps` is the port for everything the use cases need from their
  environment, and :class:`LiveNotifyOps` the live adapter.
- :class:`LegacyQueueNotifyUseCase` holds the ``notify_agent`` body.
- :class:`StandardNotifyUseCase` holds the ``_notify_standard_via_handoff`` body.
- :class:`NotifyCommandUseCase` holds the six ``cmd_notify_*`` command-entry
  bodies (Redmine #12983): the per-subcommand receiver / default-kind / legacy-flag
  entry policy that used to sit in ``commands.py``, delegating the actual work to
  the two use cases above.

The live adapter resolves every helper *through the* :mod:`commands` *module at
call time* (``require_tmux`` / ``find_handoff_task`` / ``pane_info`` /
``ensure_agent_target`` / ``cmd_read`` / ``cmd_message`` / ``cmd_keys`` /
``wait_for_text`` / ``rollback_unsubmitted_input`` / ``build_prompt`` /
``landing_marker`` / ``orchestrate_handoff`` / ``die`` / ``time.sleep``), so the
existing characterization tests that patch ``mozyo_bridge.application.commands
.<fn>`` keep intercepting the side effects unchanged and no import cycle is
introduced (``commands`` imports this module only lazily inside the thin
wrappers). This is a pure, behavior-preserving restructuring: the CLI stdout /
stderr / exit codes, the marker/rollback/Enter sequence, and the legacy success
line are all identical to the original bodies.
"""

from __future__ import annotations

import argparse
from typing import Any, Protocol

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    KIND_LABELS,
    MODE_QUEUE_ENTER,
    RECORD_FORMAT_BOTH,
)


class NotifyOps(Protocol):
    """Port: everything the notify use cases need from their environment.

    The use cases depend only on this protocol, so they are exercisable with a
    synthetic fake. The live adapter routes each call through the :mod:`commands`
    module so monkeypatched test doubles still intercept.
    """

    def require_tmux(self) -> None: ...

    def validate_notify_gate(self, args: argparse.Namespace) -> None: ...

    def find_handoff_task(self, args: argparse.Namespace, agent: str) -> Any: ...

    def load_tmux_conf_for(self, args: argparse.Namespace) -> None: ...

    def pane_info(self, target_name: str) -> dict: ...

    def ensure_agent_target(
        self, target_info: dict, agent: str, *, force: bool
    ) -> None: ...

    def cmd_read(self, args: argparse.Namespace) -> Any: ...

    def build_prompt(
        self, args: argparse.Namespace, agent: str, task: Any
    ) -> str: ...

    def cmd_message(self, args: argparse.Namespace) -> Any: ...

    def landing_marker(self, args: argparse.Namespace, task: Any) -> str: ...

    def wait_for_text(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool: ...

    def rollback_unsubmitted_input(self, target: str) -> None: ...

    def die(self, message: str) -> Any: ...

    def cmd_keys(self, args: argparse.Namespace) -> Any: ...

    def sleep(self, seconds: float) -> None: ...

    def orchestrate_handoff(self, args: argparse.Namespace) -> int: ...


class LiveNotifyOps:
    """Live :class:`NotifyOps` over the real ``commands`` helpers.

    Every method resolves its helper *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the ``notify_agent`` /
    ``notify-*`` characterization tests that patch ``mozyo_bridge.application
    .commands.<fn>`` keep intercepting the side effects and no import cycle is
    introduced.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def require_tmux(self) -> None:
        self._commands().require_tmux()

    def validate_notify_gate(self, args: argparse.Namespace) -> None:
        self._commands().validate_notify_gate(args)

    def find_handoff_task(self, args: argparse.Namespace, agent: str) -> Any:
        return self._commands().find_handoff_task(args, agent)

    def load_tmux_conf_for(self, args: argparse.Namespace) -> None:
        self._commands().load_tmux_conf_for(args)

    def pane_info(self, target_name: str) -> dict:
        return self._commands().pane_info(target_name)

    def ensure_agent_target(
        self, target_info: dict, agent: str, *, force: bool
    ) -> None:
        self._commands().ensure_agent_target(target_info, agent, force=force)

    def cmd_read(self, args: argparse.Namespace) -> Any:
        return self._commands().cmd_read(args)

    def build_prompt(self, args: argparse.Namespace, agent: str, task: Any) -> str:
        return self._commands().build_prompt(args, agent, task)

    def cmd_message(self, args: argparse.Namespace) -> Any:
        return self._commands().cmd_message(args)

    def landing_marker(self, args: argparse.Namespace, task: Any) -> str:
        return self._commands().landing_marker(args, task)

    def wait_for_text(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool:
        return self._commands().wait_for_text(target, marker, lines, timeout)

    def rollback_unsubmitted_input(self, target: str) -> None:
        self._commands().rollback_unsubmitted_input(target)

    def die(self, message: str) -> Any:
        return self._commands().die(message)

    def cmd_keys(self, args: argparse.Namespace) -> Any:
        return self._commands().cmd_keys(args)

    def sleep(self, seconds: float) -> None:
        self._commands().time.sleep(seconds)

    def orchestrate_handoff(self, args: argparse.Namespace) -> int:
        return self._commands().orchestrate_handoff(args)


class LegacyQueueNotifyUseCase:
    """The legacy-queue ``notify_agent`` body behind the :class:`NotifyOps` port.

    ``notify-*-legacy-task`` is the retired-queue cleanup wrapper: it drives the
    raw type-observe-marker-Enter TUI rail directly and intentionally does NOT
    route through ``orchestrate_handoff``, so it emits no structured durable
    record. Behavior is byte-for-byte identical to the original ``notify_agent``.
    """

    def __init__(self, ops: NotifyOps) -> None:
        self._ops = ops

    def run(self, args: argparse.Namespace, agent: str) -> int:
        ops = self._ops
        ops.require_tmux()
        ops.validate_notify_gate(args)
        task = (
            None
            if getattr(args, "journal", None)
            else ops.find_handoff_task(args, agent)
        )
        target_name = args.target or agent
        if getattr(args, "config", False):
            ops.load_tmux_conf_for(args)
        target_info = ops.pane_info(target_name)
        ops.ensure_agent_target(target_info, agent, force=args.force)
        target = target_info["id"]
        read_lines = str(args.read_lines)
        ops.cmd_read(argparse.Namespace(target=target, lines=args.read_lines))
        prompt = ops.build_prompt(args, agent, task)
        ops.cmd_message(argparse.Namespace(target=target, text=prompt, submit=False))
        ops.cmd_read(argparse.Namespace(target=target, lines=args.read_lines))
        marker = ops.landing_marker(args, task)
        landing_lines = max(args.read_lines, 200)
        if not ops.wait_for_text(target, marker, landing_lines, args.landing_timeout):
            ops.rollback_unsubmitted_input(target)
            ops.die(
                "notification marker was not observed in target pane; a C-u rollback was issued and Enter was not pressed (the receiver composer state was not verified). "
                f"target={target} marker={marker}"
            )
        submit_delay = max(0.0, float(getattr(args, "submit_delay", 0.0) or 0.0))
        if submit_delay:
            ops.sleep(submit_delay)
        ops.cmd_keys(argparse.Namespace(target=target, keys=["Enter"]))
        gate = f"task={task.get('id')}" if task else f"journal={args.journal}"
        print(f"notified {agent}: {gate} target={target} read_lines={read_lines}")
        return 0


class StandardNotifyUseCase:
    """The standard ``_notify_standard_via_handoff`` body behind the port.

    Maps the legacy Redmine-shaped CLI flags onto ``orchestrate_handoff``'s
    normalized contract so the standard notify path shares a single orchestration
    rail with ``mozyo-bridge handoff`` / ``mozyo-bridge reply``. Legacy queue
    notifications (``notify-*-legacy-task``) intentionally stay on
    :class:`LegacyQueueNotifyUseCase`; they remain wrapper-only cleanup paths, not
    the standard path. Behavior is byte-for-byte identical to the original
    ``_notify_standard_via_handoff``.
    """

    def __init__(self, ops: NotifyOps) -> None:
        self._ops = ops

    def run(
        self, args: argparse.Namespace, agent: str, default_kind: str
    ) -> int:
        ops = self._ops
        ops.validate_notify_gate(args)
        type_str = getattr(args, "type", None)
        if type_str in KIND_LABELS:
            kind = type_str
            summary = None
        else:
            kind = default_kind
            summary = f"legacy --type={type_str}" if type_str else None
        forwarded = argparse.Namespace(
            to=agent,
            source="redmine",
            kind=kind,
            issue=getattr(args, "issue", None),
            journal=getattr(args, "journal", None),
            task_id=None,
            comment_id=None,
            anchor_url=None,
            target=getattr(args, "target", None),
            mode=MODE_QUEUE_ENTER,
            summary=summary,
            force=bool(getattr(args, "force", False)),
            landing_timeout=float(getattr(args, "landing_timeout", 8.0) or 8.0),
            submit_delay=float(getattr(args, "submit_delay", 0.2) or 0.0),
            read_lines=int(getattr(args, "read_lines", 50) or 50),
            record_format=getattr(args, "record_format", RECORD_FORMAT_BOTH),
            record_command=getattr(args, "record_command", None),
        )
        rc = ops.orchestrate_handoff(forwarded)
        # Preserve the legacy success line so external scripts and the in-repo
        # smoke (`smoke/real_tmux_notify_smoke.py`) that grep `notified <agent>:
        # journal=...` keep working. The new primitive owns the durable record
        # and structured outcome; this wrapper line is purely a back-compat
        # courtesy and only fires on successful return from orchestrate_handoff
        # (which dies on marker_timeout, so failure paths never reach this).
        if rc == 0:
            try:
                target = ops.pane_info(getattr(args, "target", None) or agent)["id"]
            except SystemExit:
                target = "-"
            read_lines = int(getattr(args, "read_lines", 50) or 50)
            journal = getattr(args, "journal", None)
            print(f"notified {agent}: journal={journal} target={target} read_lines={read_lines}")
        return rc


class NotifyCommandUseCase:
    """The six ``notify-*`` CLI command entry bodies behind the :class:`NotifyOps` port.

    The ``notify-*`` command family historically kept its *entry policy* — the
    receiver agent, the default handoff kind, and the small legacy-flag
    normalizations — as thin procedural wrappers in ``application/commands.py``
    (``_notify_standard_via_handoff`` plus the six ``cmd_notify_*`` functions).
    This carves that residual entry layer into the notify boundary under #12983
    while preserving the public ``commands.cmd_notify_*`` identity and the legacy
    task behavior:

    - ``run_codex`` / ``run_claude`` / ``run_codex_review`` /
      ``run_claude_review_result`` route the *standard* subcommands through
      :class:`StandardNotifyUseCase`, i.e. the shared high-level
      ``orchestrate_handoff`` rail. The two ``*_review*`` variants first pin
      ``args.type`` to the legacy Redmine-shaped token so the type -> kind mapping
      resolves the reviewed kind while still recording the legacy summary rule.
    - ``run_codex_legacy_task`` / ``run_claude_legacy_task`` reset ``args.journal``
      to ``None`` and route through :class:`LegacyQueueNotifyUseCase`, i.e. the
      retired-queue type-observe-marker-Enter cleanup path. These stay
      wrapper-only cleanup paths and do NOT become the standard transport.

    Behavior is byte-for-byte identical to the original ``cmd_notify_*`` wrappers:
    the same receiver, default kind, ``args.type`` pinning, ``args.journal`` reset,
    orchestration rail, and success lines. The two inner use cases share this
    instance's single :class:`NotifyOps`, so the live ``commands.*`` monkeypatch
    seams they resolve at call time stay intact.
    """

    def __init__(self, ops: NotifyOps) -> None:
        self._ops = ops

    def _notify_standard(
        self, args: argparse.Namespace, agent: str, default_kind: str
    ) -> int:
        # Formerly ``commands._notify_standard_via_handoff``: map the legacy
        # Redmine-shaped ``notify-*`` flags onto ``orchestrate_handoff``'s
        # normalized contract so the standard notify path shares a single
        # orchestration rail with ``mozyo-bridge handoff`` / ``reply``.
        return StandardNotifyUseCase(self._ops).run(args, agent, default_kind)

    def _notify_legacy(self, args: argparse.Namespace, agent: str) -> int:
        # Formerly ``commands.notify_agent``: drive the raw
        # type-observe-marker-Enter TUI rail directly. This wrapper-only cleanup
        # path intentionally does NOT route through ``orchestrate_handoff`` and
        # emits no structured durable record.
        return LegacyQueueNotifyUseCase(self._ops).run(args, agent)

    def run_codex(self, args: argparse.Namespace) -> int:
        return self._notify_standard(args, "codex", default_kind="reply")

    def run_claude(self, args: argparse.Namespace) -> int:
        return self._notify_standard(args, "claude", default_kind="reply")

    def run_codex_review(self, args: argparse.Namespace) -> int:
        args.type = "review_request"
        return self._notify_standard(args, "codex", default_kind="review_request")

    def run_claude_review_result(self, args: argparse.Namespace) -> int:
        args.type = "review_result"
        return self._notify_standard(args, "claude", default_kind="review_result")

    def run_codex_legacy_task(self, args: argparse.Namespace) -> int:
        args.journal = None
        return self._notify_legacy(args, "codex")

    def run_claude_legacy_task(self, args: argparse.Namespace) -> int:
        args.journal = None
        return self._notify_legacy(args, "claude")
