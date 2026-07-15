"""Backend-aware bare ``mozyo`` — the herdr branch (Redmine #13324).

Historically bare ``mozyo`` (no subcommand) meant one thing: *this project's*
tmux cockpit — ensure a repo-scoped session with a ``claude`` + ``codex`` window
and attach. That whole flow lives in :mod:`mozyo_bridge.application.launch_command`
(``MozyoLaunchUseCase`` + ``deliver_mozyo_launch_outcome``) and is left **untouched**
here: a repo whose ``terminal_transport.backend`` is ``tmux`` / unset / has no
config file keeps the byte-invariant tmux path.

When a repo opts into the herdr backend (``terminal_transport.backend: herdr`` in
``.mozyo-bridge/config.yaml``), the *project-start entrypoint* should still be one
command. herdr-main operation had split it into two — ``mozyo-bridge herdr
session-start`` (launch/adopt the claude+codex slots) then ``herdr`` (attach the
UI). This module restores the single-command semantics for the herdr backend by
composing the existing session-start use case with a herdr-UI ``exec`` attach,
while the 2-command flow keeps working unchanged.

Design (auditor ruling, Redmine #13324 j#73153):

- **backend selection** happens at the entrypoint (``cli.main``): only the
  resolved repo root's repo-local ``terminal_transport.backend`` chooses this
  branch. A present-but-broken config already fails closed *before* the branch
  (``load_repo_local_config``), so this module is only reached with a valid herdr
  selection.
- **session-start reuse**: the herdr slots are prepared through the existing
  :func:`prepare_session` (idempotent adopt/launch, self-identity ``--env``
  injection); nothing here re-implements the launch mechanics.
- **(a) attach**: the default execs the resolved ``MOZYO_HERDR_BINARY`` herdr
  client — the same executable the trusted-env resolver (#13245) validates — so
  the operator lands in the herdr UI. The ``exec`` / ``in_tmux`` / ``prepare`` /
  ``resolve_binary`` side effects all sit behind the :class:`HerdrLaunchOps` port
  so the whole flow is exercisable with a synthetic fake.
- **(b) tmux-nested guard**: a real attach (``exec``) from *inside* tmux would
  nest a full-screen TUI inside a tmux pane; when ``$TMUX`` is set and the run
  would attach, this fails closed *before* session-start with actionable text
  (run outside tmux, or use ``--no-attach`` / ``--json`` for session-start only).
  ``--no-attach`` / ``--json`` never attach, so they are allowed inside tmux.
- **(c) flags**: ``--repo`` is backend-neutral and honored; ``--no-attach`` runs
  session-start only and prints the attach hint; ``--json`` is an effective
  no-attach that emits the machine-readable payload; ``--cc`` / ``--session`` are
  tmux-only and are **rejected explicitly** (never silently ignored) on the herdr
  backend.
- **(d) fail-closed binary / server**: an unconfigured / unresolvable
  ``MOZYO_HERDR_BINARY`` (reusing the #13245 ``refusing to fall back to tmux``
  wording) or any session-start failure (server unreachable, non-zero, timeout)
  fails closed — never a silent tmux fallback.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn, Protocol, runtime_checkable

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    HerdrSessionStartError,
    SessionStartResult,
    prepare_session,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.default_agent_topology import (
    DEFAULT_EXPECTED_AGENTS,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    TerminalTransportConfig,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    resolve_terminal_transport,
)


# --- Pure policy: attach hint, ready flag, JSON payload, text block. ----------

#: The provider slots bare ``mozyo`` prepares — one herdr agent per provider,
#: matching the tmux path's ``claude`` + ``codex`` windows. Sourced from the single
#: canonical default-topology contract (Redmine #13569) so the herdr launch pair and
#: the tmux status/doctor "expected" judgment can never drift.
LAUNCH_PROVIDERS: tuple[str, ...] = DEFAULT_EXPECTED_AGENTS


def herdr_attach_command_line(binary: str) -> str:
    """The herdr-UI attach hint (the command that enters the herdr client).

    Attaching to the herdr UI is running the herdr client itself; the resolved
    ``MOZYO_HERDR_BINARY`` *is* that client, so the hint is the resolved binary.
    """

    return binary


def herdr_attach_argv(binary: str) -> list[str]:
    """The ``os.execvp`` argv for entering the herdr UI (the bare client)."""

    return [binary]


def session_ready(result: SessionStartResult) -> bool:
    """True iff every requested slot resolved to a live (adopted/launched) locator.

    :func:`prepare_session` fails closed on any slot it cannot prepare, so a
    returned result is normally fully ready; this stays an explicit predicate
    (rather than assuming) so the ``--json`` ``ready`` flag reflects the observed
    slot outcomes, mirroring the tmux path's ``ready`` (both agent windows
    present).
    """

    slots = list(result.slots)
    return bool(slots) and all(
        slot.outcome in {SLOT_ADOPTED, SLOT_LAUNCHED} and bool(slot.locator)
        for slot in slots
    )


def build_herdr_json_payload(
    *, result: SessionStartResult, attach_command: str
) -> dict[str, Any]:
    """The backend-aware ``mozyo --json`` payload on the herdr backend (#13324).

    ``--json`` never attaches, so ``attached`` is always ``False`` and
    ``no_attach`` is the *effective* value (always ``True`` here), matching the
    tmux path's effective-``no_attach`` reporting. ``session_start`` carries the
    full session-start outcome (workspace / lane / per-slot names + locators).
    """

    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (
        HERDR_STANDARD_DISPATCH_HINT,
    )

    return {
        "backend": BACKEND_HERDR,
        "ready": session_ready(result),
        "session_start": result.as_payload(),
        "attach": attach_command,
        "attached": False,
        "no_attach": True,
        # Redmine #13446: the standard next-action pointer, so `mozyo --json` confirms the
        # herdr workspace/agents AND names the safe lane-dispatch surface (`sublane create
        # --execute`) rather than leaving the tmux-era selection primitives as the apparent
        # entrypoint.
        "next_action": HERDR_STANDARD_DISPATCH_HINT,
    }


def render_herdr_session_block(
    result: SessionStartResult, attach_command: str
) -> str:
    """The human text block printed before attaching / as the no-attach summary.

    Reuses the session-start slot summary (``workspace`` / ``lane`` / per-slot
    outcome + durable name + locator) and appends the herdr-UI attach hint line,
    so ``--no-attach`` prints exactly what the operator needs to attach later.
    """

    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (
        HERDR_STANDARD_DISPATCH_HINT,
    )

    lines = [
        f"herdr session-start: workspace={result.workspace_id} lane={result.lane_id}"
    ]
    for slot in result.slots:
        line = f"  - {slot.provider}: {slot.outcome} name={slot.assigned_name}"
        if slot.locator:
            line += f" locator={slot.locator}"
        lines.append(line)
    lines.append(f"attach: {attach_command}")
    # Redmine #13446: name the standard lane-dispatch next action so the confirmed herdr
    # workspace/agents summary also tells the operator the safe next step (not the tmux-era
    # selection primitives).
    lines.append(f"next: {HERDR_STANDARD_DISPATCH_HINT}")
    return "\n".join(lines)


# --- Port + live adapter ------------------------------------------------------


@runtime_checkable
class HerdrLaunchOps(Protocol):
    """Port: everything the herdr launch use case needs from its environment.

    The live adapter binds these to the real trusted-env binary resolution, the
    ``$TMUX`` probe, the :func:`prepare_session` side effect, the terminal
    ``os.execvp`` attach, and stdout / ``die`` — so a synthetic fake pins the
    whole decision + delivery without a live herdr binary.
    """

    def repo_root(self, args: argparse.Namespace) -> Path: ...

    def in_tmux(self) -> bool: ...

    def resolve_binary(self) -> str: ...

    def prepare(self, repo_root: Path) -> SessionStartResult: ...

    def attach(self, argv: list[str]) -> NoReturn: ...

    def emit(self, text: str, end: str = "\n") -> None: ...

    def die(self, message: str) -> NoReturn: ...


class LiveHerdrLaunchOps:
    """Live :class:`HerdrLaunchOps` over the real trusted-env + herdr helpers."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        # Default to the ambient process environment; injectable for tests.
        self._env: Mapping[str, str] = env if env is not None else os.environ

    def repo_root(self, args: argparse.Namespace) -> Path:
        from mozyo_bridge.application.commands_common import repo_root_from_args

        return repo_root_from_args(args)

    def in_tmux(self) -> bool:
        # Same probe the doctor uses (#12612): either variable marks a tmux client.
        return bool(self._env.get("TMUX") or self._env.get("TMUX_PANE"))

    def resolve_binary(self) -> str:
        # Reuse the single #13245 fail-closed resolver so the unconfigured /
        # unresolvable wording ("refusing to fall back to tmux") stays one source
        # of truth; the herdr backend is enabled here, so this never returns None.
        transport = resolve_terminal_transport(
            TerminalTransportConfig(backend=BACKEND_HERDR), env=self._env
        )
        assert transport is not None  # herdr selected -> never the tmux-off None
        return transport.binary

    def prepare(self, repo_root: Path) -> SessionStartResult:
        # Prepare both provider slots in the default (no-lane) session, reusing the
        # existing idempotent adopt/launch + self-identity `--env` injection.
        #
        # Redmine #13397 (finding 3): this is the bare `mozyo` COORDINATOR pair launch
        # (claude + codex, default no-lane session). #13360 gave the herdr LANE worker
        # `--permission-mode` parity with the tmux managed pane but deliberately left
        # bare `mozyo` flagless — which leaves an external-project coordinator's Claude
        # booting prompt-gated (manual mode) and unusable headless, the asymmetry the
        # #13379 j#73721 finding 3 recorded. Passing the same cockpit/sublane policy
        # default (`auto`) gives the coordinator Claude the identical reproducible
        # permission posture the lane worker already has. The `MOZYO_CLAUDE_PERMISSION_MODE`
        # env override still wins for an operator who wants a different mode, and Codex
        # is untouched (the policy is claude-only).
        # Config-driven launch argv (Redmine #13425): the bare `mozyo` coordinator pair is
        # the `default` lane_class, so the config's `launch_argv.{provider}.default` tokens
        # (e.g. claude `--model`, codex `--config model_reasoning_effort=xhigh`) are
        # appended at the launch chokepoint. Unconfigured repos are byte-for-byte unchanged.
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        # Config-driven pane placement (Redmine #13646): the bare `mozyo` coordinator pair
        # is the `default` lane_class, so the config's `lane_placement.default` split /
        # order decides the pair's geometry (e.g. `split: down` for the owner's vertical
        # main window) and which provider occupies first. Unconfigured repos keep the herdr
        # server default placement and the requested provider order, byte-for-byte.
        repo_config = load_repo_local_config(repo_root)
        return prepare_session(
            repo_root=repo_root,
            providers=list(LAUNCH_PROVIDERS),
            lane_id="",
            env=self._env,
            claude_permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
            agent_launch=repo_config.agent_launch,
            lane_placement=repo_config.lane_placement,
        )

    def attach(self, argv: list[str]) -> NoReturn:
        os.execvp(argv[0], argv)
        raise AssertionError("unreachable")  # pragma: no cover - execvp replaces process

    def emit(self, text: str, end: str = "\n") -> None:
        print(text, end=end)

    def die(self, message: str) -> NoReturn:
        from mozyo_bridge.shared.errors import die

        die(message)
        raise AssertionError("unreachable")  # pragma: no cover - die raises SystemExit


# --- Outcome ------------------------------------------------------------------


@dataclass(frozen=True)
class HerdrLaunchOutcome:
    """Result of :class:`MozyoHerdrLaunchUseCase` — a refusal, JSON, or attach plan.

    ``error_message`` is the bare ``die`` message (fail-closed, non-zero exit).
    ``json_stdout`` is the single ``--json`` block. On the text path
    ``pre_attach_text`` is the session summary printed before attaching; when
    ``no_attach`` it *is* the summary (the attach hint line is already folded in)
    and no exec follows. ``attach_argv`` is the herdr-UI ``os.execvp`` argv.
    """

    error_message: str | None = None
    json_stdout: str | None = None
    pre_attach_text: str | None = None
    attach_argv: tuple[str, ...] = ()
    no_attach: bool = False


# --- Use case -----------------------------------------------------------------


@dataclass
class MozyoHerdrLaunchUseCase:
    """Backend-aware bare ``mozyo`` on the herdr backend over :class:`HerdrLaunchOps`.

    Decides refusal vs JSON vs no-attach vs attach; the terminal ``os.execvp``
    attach and stdout stay in :func:`deliver_herdr_launch_outcome`.
    """

    ops: HerdrLaunchOps

    def run(self, args: argparse.Namespace) -> HerdrLaunchOutcome:
        ops = self.ops

        # (c) tmux-only flags are rejected explicitly on the herdr backend — a
        # silent ignore would let an operator believe `--cc` / `--session` took
        # effect when the herdr UI has no tmux control mode or session name.
        if bool(getattr(args, "cc", False)):
            return HerdrLaunchOutcome(
                error_message=(
                    "`--cc` (iTerm2 tmux control mode) is a tmux-only flag and has "
                    "no meaning on the herdr backend "
                    "(terminal_transport.backend: herdr); remove it."
                )
            )
        if getattr(args, "session", None) is not None:
            return HerdrLaunchOutcome(
                error_message=(
                    "`--session` names a tmux session and has no meaning on the "
                    "herdr backend (terminal_transport.backend: herdr); herdr "
                    "identities are the durable agent names — remove it."
                )
            )

        json_output = bool(getattr(args, "json_output", False))
        no_attach = bool(getattr(args, "no_attach", False))
        # `--json` is an effective no-attach (a launcher capturing stdout is never
        # replaced by an exec), matching the tmux path.
        effective_attach = not (no_attach or json_output)

        # (b) a real attach from inside tmux would nest the herdr TUI inside a tmux
        # pane; fail closed BEFORE any session-start side effect.
        if effective_attach and ops.in_tmux():
            return HerdrLaunchOutcome(
                error_message=(
                    "refusing to attach the herdr UI from inside tmux ($TMUX is "
                    "set): a full-screen herdr client nested in a tmux pane is "
                    "unusable. Run `mozyo` from a terminal outside tmux to attach, "
                    "or use `mozyo --no-attach` / `mozyo --json` to run herdr "
                    "session-start only."
                )
            )

        repo_root = ops.repo_root(args)

        # (a)/(d) resolve the herdr client up front so an unconfigured /
        # unresolvable MOZYO_HERDR_BINARY fails closed (no tmux fallback) before
        # session-start, and so the attach hint / exec argv use the resolved path.
        try:
            binary = ops.resolve_binary()
        except TerminalTransportError as exc:
            return HerdrLaunchOutcome(error_message=str(exc))

        # Reuse the existing session-start (idempotent adopt/launch + self-identity
        # env). Any fail-closed condition (server unreachable, non-zero, timeout,
        # duplicate identity) stays fail-closed — never a silent tmux fallback.
        try:
            result = ops.prepare(repo_root)
        except HerdrSessionStartError as exc:
            return HerdrLaunchOutcome(
                error_message=(
                    f"herdr session-start failed: {exc}; refusing to fall back to tmux"
                )
            )

        attach_command = herdr_attach_command_line(binary)

        if json_output:
            payload = build_herdr_json_payload(
                result=result, attach_command=attach_command
            )
            return HerdrLaunchOutcome(
                json_stdout=json.dumps(
                    payload, ensure_ascii=False, indent=2, sort_keys=True
                )
            )

        return HerdrLaunchOutcome(
            pre_attach_text=render_herdr_session_block(result, attach_command),
            attach_argv=tuple(herdr_attach_argv(binary)),
            no_attach=no_attach,
        )


# --- Terminal delivery --------------------------------------------------------


def deliver_herdr_launch_outcome(
    outcome: HerdrLaunchOutcome, ops: HerdrLaunchOps
) -> int:
    """Render a :class:`HerdrLaunchOutcome` to the terminal and attach.

    A refusal dies first (non-zero exit); the ``--json`` block short-circuits;
    otherwise the session summary prints and — unless ``--no-attach`` — the port's
    ``attach`` execs the herdr client, replacing the process.
    """

    if outcome.error_message is not None:
        ops.die(outcome.error_message)
    if outcome.json_stdout is not None:
        ops.emit(outcome.json_stdout)
        return 0
    if outcome.pre_attach_text is not None:
        ops.emit(outcome.pre_attach_text)
    if outcome.no_attach:
        return 0
    ops.attach(list(outcome.attach_argv))
    raise AssertionError("unreachable")  # pragma: no cover - attach never returns


def cmd_mozyo_herdr(args: argparse.Namespace) -> int:
    """CLI entry: backend-aware bare ``mozyo`` on the herdr backend (#13324).

    Parser-bound wrapper over run + deliver, mirroring the tmux ``cmd_mozyo``
    shape. Reached only from :func:`mozyo_bridge.application.cli.main` when the
    resolved repo's ``terminal_transport.backend`` is ``herdr``.
    """

    ops = LiveHerdrLaunchOps()
    return deliver_herdr_launch_outcome(MozyoHerdrLaunchUseCase(ops).run(args), ops)


__all__ = (
    "LAUNCH_PROVIDERS",
    "HerdrLaunchOps",
    "HerdrLaunchOutcome",
    "LiveHerdrLaunchOps",
    "MozyoHerdrLaunchUseCase",
    "build_herdr_json_payload",
    "cmd_mozyo_herdr",
    "deliver_herdr_launch_outcome",
    "herdr_attach_argv",
    "herdr_attach_command_line",
    "render_herdr_session_block",
    "session_ready",
)
