"""Isolated-home + scratch-workspace rail for deterministic *installed* fault paths (#14097).

The four fault shapes this repo pins for release verification — post-close stale-worker
resume (#13806), the nested unhealthy-launch rollback pointer (#13948), the stale-locator
``sublane list`` projection (#14063), and callback-sweep lease recovery (#13951) — each
already have deterministic regressions, but every one of those drives its use case / store /
domain fold through **internal module imports**. None routes ``argv`` through the *public*
CLI dispatch (``build_parser() -> args.func``), which is exactly the surface the installed
``mozyo-bridge`` binary runs. This harness closes that gap:

- it drives the SAME public command dispatch the installed CLI runs (the real argparse tree
  + the real ``cmd_*`` handlers), so the scenario measures the public orchestration path, not
  an internal seam;
- it confines every side effect to an **isolated ``MOZYO_BRIDGE_HOME``** and a **scratch
  herdr workspace / process** (a :class:`~tests.support.herdr_fake.FakeHerdr` over the
  subprocess boundary), so no managed lane, callback row, or lease is ever touched — the
  boundary the #14097 Acceptance requires;
- it prepares each fault only through the safe fixture rails the isolated home already owns
  (the home-scoped public stores + the fake's one-shot stimuli), so an operator/agent driving
  the harness never issues a raw SQLite / tmux / Herdr mutation.

It is deliberately NOT a subprocess of the operator's ``pipx``-installed binary: that would
be non-hermetic (it measures whatever artifact is installed, not the source under review) and
belongs to the release / container smoke, not the CI-hermetic ``tests/scenarios`` suite. The
public *dispatch* — the parser and the ``cmd_*`` handlers — is byte-identical to what the
installed entrypoint calls, so the coverage is the installed public surface; only the process
boundary is in-process.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional
from unittest import mock

from mozyo_bridge.core.state.lane_metadata import record_lane_created
from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

from tests.support.herdr_fake import (
    DEFAULT_START_STATUS,
    STATUS_WORKING,
    FakeHerdr,
)

#: A herdr ``agent_status`` that :func:`classify_named_slot` reads as a #13518 shell residue
#: (``map_agent_status`` degrades an unrecognised token to ``unknown`` -> ``RUNTIME_UNKNOWN``
#: -> :data:`SLOT_STALE`). Seeding a slot with this status reproduces a locator-present but
#: agent-absent reboot residue WITHOUT any raw inventory mutation.
STALE_SLOT_STATUS = "unknown"

#: The coordinator lane the harness's own workspace sits in. Kept distinct from every scratch
#: sublane id so the fold never confuses the driver's own coordinator pair with a scratch lane.
COORDINATOR_LANE = "default"


class CliResult(NamedTuple):
    """The captured outcome of one public CLI dispatch (exit code + streams)."""

    rc: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        """Parse ``stdout`` as the command's ``--json`` payload (fails loudly if it is not)."""
        return json.loads(self.stdout)


class _HerdrRunner:
    """Route the driven subprocess calls of a public command hermetically (#14097).

    A public command under the herdr backend shells out to three consumers: the herdr binary
    (-> the shared :class:`FakeHerdr`), the git-topology probe of ``herdr_workspace_segment``
    (-> *not a git repo*, the pure-herdr / external posture the scratch root models), and — on
    the tmux fallback only — ``command -v tmux`` (-> *no tmux*). Any other argv is unexpected
    and raises, preserving the fail-closed posture (no silent canned success).
    """

    def __init__(self, fake: FakeHerdr, herdr_bin: str) -> None:
        self.fake = fake
        self.herdr_bin = herdr_bin

    def run(self, argv, **kwargs):  # noqa: ANN001 - subprocess.run signature
        head = str(argv[0])
        if head == self.herdr_bin:
            return self.fake.run(argv, **kwargs)
        if head == "git" or head.endswith("/git"):
            # The scratch root is a plain directory, not a git checkout: every git probe
            # (rev-parse / worktree list) reports "not a work tree" the way an external
            # herdr-only project does.
            return subprocess.CompletedProcess(list(argv), 128, stdout="", stderr="not a git repository")
        if head in ("sh", "/bin/sh", "bash", "/bin/bash"):
            # ``require_tmux`` runs ``sh -c 'command -v tmux'``; report tmux absent so a
            # misrouted send fails closed exactly as a pure-herdr session would.
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess in installed fault harness: {argv!r}")

    def popen(self, argv, **kwargs):  # noqa: ANN001 - subprocess.Popen signature
        head = str(argv[0])
        if head == self.herdr_bin:
            return self.fake.popen(argv, **kwargs)
        raise AssertionError(f"unexpected Popen in installed fault harness: {argv!r}")


class InstalledFaultHarness:
    """A registered coordinator workspace + isolated home that drives the public CLI.

    Register for cleanup on a :class:`unittest.TestCase`; :meth:`run_cli` dispatches one public
    command exactly as the installed binary would, under the isolated home + fake herdr. Scratch
    lanes are seeded with :meth:`seed_lane`; the fault stimuli (a stale slot, a vanished pair)
    are set at seed time so the fold observes a locator-present-but-agent-absent residue with no
    raw inventory mutation.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        self._case = case
        self._tmp = Path(tempfile.mkdtemp()).resolve()
        case.addCleanup(shutil.rmtree, self._tmp, True)

        self.home = self._tmp / "home"
        self.home.mkdir()

        # A registered herdr-backend coordinator workspace (config-only marker, no ``.git`` —
        # the external / pure-herdr posture the fake models). Its registry ``workspace_id`` is
        # the segment every seeded scratch slot is keyed on.
        self.repo_root = self._tmp / "project"
        (self.repo_root / ".mozyo-bridge").mkdir(parents=True)
        (self.repo_root / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
        )
        register_workspace(self.repo_root, home=self.home)
        self.workspace_id = read_anchor(self.repo_root)["workspace_id"]

        # A resolvable, executable fake herdr binary (the resolver requires an executable file;
        # the runner routes argv[0] == this to the fake).
        herdr_bin_path = self._tmp / "fake-herdr"
        herdr_bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        herdr_bin_path.chmod(0o755)
        self.herdr_bin = str(herdr_bin_path)

        # A readable-but-EMPTY composer read (a rendered prompt line ``> `` with no body): a
        # fresh idle launch has nothing typed, so a rollback's "pending input cannot be ruled
        # out" guard clears (a blank read would instead read as *unreadable*) and the fresh pane
        # is closeable.
        self.fake = FakeHerdr(read_text="idle\n> ")
        self._ws = self.fake.seed_workspace(cwd=str(self.repo_root))
        self._runner = _HerdrRunner(self.fake, self.herdr_bin)
        #: assigned-name -> seeded locator, for callers that re-reference a seeded slot.
        self._locators: dict[str, str] = {}

    # -- environment ----------------------------------------------------------

    def _env(self) -> dict:
        """The attested coordinator-driver identity env for a public dispatch."""
        import os

        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.pop("MOZYO_REPO", None)
        env["MOZYO_HERDR_BINARY"] = self.herdr_bin
        env["MOZYO_BRIDGE_HOME"] = str(self.home)
        env["MOZYO_WORKSPACE_ID"] = self.workspace_id
        env["MOZYO_AGENT_ROLE"] = "codex"
        env["MOZYO_LANE_ID"] = COORDINATOR_LANE
        return env

    @contextlib.contextmanager
    def _driving_context(self):
        import os

        prev_cwd = os.getcwd()
        os.chdir(self.repo_root)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch("subprocess.run", self._runner.run))
                stack.enter_context(mock.patch("subprocess.Popen", self._runner.popen))
                stack.enter_context(mock.patch.dict(os.environ, self._env(), clear=True))
                yield stack
        finally:
            os.chdir(prev_cwd)

    # -- public CLI dispatch --------------------------------------------------

    def run_cli(self, argv: list[str]) -> CliResult:
        """Dispatch one public command the way the installed ``mozyo-bridge`` binary does.

        Builds the REAL argparse tree, parses ``argv``, and calls the resolved ``cmd_*`` handler
        under the isolated home + fake herdr + scratch cwd. Returns the exit code (a handler that
        returns ``None`` is a success 0) and the captured streams.
        """
        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with self._driving_context():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = args.func(args)
        return CliResult(int(rc or 0), out.getvalue(), err.getvalue())

    # -- scratch lane seeding (safe fixture rail) -----------------------------

    def seed_lane(
        self,
        lane_id: str,
        *,
        issue: str,
        gateway: str = "live",
        worker: str = "live",
        branch: str = "",
    ) -> dict[str, str]:
        """Seed one scratch sublane's ``codex`` / ``claude`` slots + its lane metadata record.

        ``gateway`` / ``worker`` are one of ``"live"`` (a working managed agent), ``"stale"``
        (a locator-present shell residue — the #13518 / #14063 fault), or ``"absent"`` (no slot
        row at all). The lane is keyed on ``(workspace_id, lane_id)`` exactly as ``sublane
        create`` keys a shared-model lane, so the public ``sublane list`` fold groups it as one
        lane unit. Returns the seeded locators (``{}`` value for an absent slot).
        """
        record_lane_created(
            lane_workspace_token=lane_id,
            repo_workspace_id=self.workspace_id,
            issue_id=issue,
            lane_label=lane_id,
            branch=branch or f"branch_{lane_id}",
            lane_id=lane_id,
            source_backend="herdr",
            home=self.home,
        )
        locators: dict[str, str] = {}
        for role, disposition in (("codex", gateway), ("claude", worker)):
            if disposition == "absent":
                continue
            status = STALE_SLOT_STATUS if disposition == "stale" else STATUS_WORKING
            name = encode_assigned_name(self.workspace_id, role, lane_id)
            locators[role] = self.fake.seed_agent(
                name,
                workspace_id=self._ws,
                provider="" if disposition == "stale" else role,
                status=status,
            )
            self._locators[name] = locators[role]
        return locators

    def seed_stale_worker(self, lane_id: str, *, role: str = "claude") -> str:
        """Seed a single locator-present shell-residue worker + its lane record; return its name.

        The lane id embeds the owning issue (``issue_<id>_...``) so the recover-stale preflight's
        issue/lane match resolves. Used by shape 1 (stale-worker recovery preflight).
        """
        record_lane_created(
            lane_workspace_token=lane_id,
            repo_workspace_id=self.workspace_id,
            issue_id=lane_id.split("_")[1] if "_" in lane_id else "",
            lane_label=lane_id,
            branch=f"branch_{lane_id}",
            lane_id=lane_id,
            source_backend="herdr",
            home=self.home,
        )
        name = encode_assigned_name(self.workspace_id, role, lane_id)
        self._locators[name] = self.fake.seed_agent(
            name, workspace_id=self._ws, provider="", status=STALE_SLOT_STATUS
        )
        return name

    def locator_of(self, assigned_name: str) -> str:
        """The locator a prior seed placed for ``assigned_name``."""
        return self._locators[assigned_name]

    # -- callback-sweep lease fixture rail (#13951) ---------------------------

    def lease_store(self):
        """The home-scoped ``CallbackSweepLease`` the public ``workflow callback-lease`` resolves.

        Same isolated path as the CLI (both resolve through ``MOZYO_BRIDGE_HOME``), so a fixture
        step (acquire / expire / drop the DB) prepares exactly the store the measured command then
        diagnoses. This is the safe isolated fixture rail: the typed home-scoped store API plus
        file operations on the scratch artifacts — never raw SQL against a live lease.
        """
        from mozyo_bridge.core.state.callback_sweep_lease import CallbackSweepLease

        return CallbackSweepLease(home=self.home)

    def callback_lease_cli(self, *flags: str) -> CliResult:
        """Dispatch ``workflow callback-lease`` (status when no flag) through the public CLI."""
        return self.run_cli(["workflow", "callback-lease", *flags])

    def run_lease_apply_with_failing_backup_cleanup(self, fingerprint: str) -> CliResult:
        """Drive the public lease apply with a mid-backup mutation + a failing backup cleanup.

        Reproduces the one non-zero-write outcome the rail reports honestly: a concurrent
        mutation lands mid-backup so the apply must roll back the copies it just wrote, and the
        cleanup ``unlink`` of those copies fails — leaving a residue the command must name
        (``rollback_incomplete``, ``zero_write=False``) rather than hide. The injection is scoped
        entirely to this dispatch (a test-only patch of the store's own backup step + ``unlink``);
        it adds NO always-on fault path to production. It patches at the class level because the
        public command constructs its own :class:`CallbackSweepLease`.
        """
        from mozyo_bridge.core.state.callback_sweep_lease import CallbackSweepLease

        original_backup = CallbackSweepLease._backup_artifacts
        real_unlink = Path.unlink

        def mutate_mid_backup(lease_self, recovery_id):
            result = original_backup(lease_self, recovery_id)
            # A concurrent process swaps the sidecar after the backup was taken.
            lease_self.sidecar_path.write_text("mutated-during-backup", encoding="utf-8")
            return result

        def failing_unlink(path_self, *a, **k):
            if "recovery-backup" in path_self.name:
                raise OSError("permission denied")
            return real_unlink(path_self, *a, **k)

        with mock.patch.object(CallbackSweepLease, "_backup_artifacts", mutate_mid_backup):
            with mock.patch.object(Path, "unlink", failing_unlink):
                return self.callback_lease_cli(
                    "--recover", "--apply", "--expect-fingerprint", fingerprint
                )

    @staticmethod
    def lease_fingerprint_from(result: CliResult) -> str:
        """Extract the ``fingerprint:`` the status / recovery command printed (redaction-safe)."""
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("fingerprint:"):
                return stripped.split(":", 1)[1].strip()
        raise AssertionError(f"no fingerprint line in callback-lease output:\n{result.stdout}")

    # -- startup-rollback fence fixture rail (#13948) -------------------------

    def seed_owed_rollback(
        self,
        lane_id: str,
        *,
        providers: tuple[str, ...] = ("claude",),
        nonce: str = "n1",
        busy: bool = False,
    ) -> tuple[str, dict[str, str]]:
        """Reserve a startup action + record a fresh unhealthy launch that owes a rollback.

        Returns ``(action_id, {role: locator})``. Each recorded participant is also placed live
        in the fake inventory so the public ``herdr session-rollback`` preflight finds it as a
        closeable fresh launch of THIS action. ``busy=True`` seeds the fresh slot as an in-flight
        turn instead (a rollback must refuse to interrupt it). This is the safe isolated fixture
        rail: the home-scoped :class:`StartupTransactionFence` public API — the same store the
        public rollback command reads — never a raw fence mutation.
        """
        from mozyo_bridge.core.state.startup_transaction_fence import (
            Participant,
            StartupTransactionFence,
            StartupUnit,
        )

        fence = StartupTransactionFence(home=self.home)
        unit = StartupUnit(workspace_id=self.workspace_id, lane_id=lane_id, providers=providers)
        action = fence.reserve(unit, nonce)
        # A fresh launch that did NOT come up healthy sits idle (never attested), the closeable
        # rollback candidate; a busy slot is an in-flight turn a rollback always refuses.
        status = STATUS_WORKING if busy else DEFAULT_START_STATUS
        locators: dict[str, str] = {}
        for role in providers:
            name = encode_assigned_name(self.workspace_id, role, lane_id)
            locator = self.fake.seed_agent(
                name, workspace_id=self._ws, provider=role, status=status
            )
            fence.record_participant(
                action.action_id,
                Participant(role=role, assigned_name=name, locator=locator, receipt=locator),
            )
            locators[role] = locator
            self._locators[name] = locator
        return action.action_id, locators

    # -- stale-worker recovery driving (#13806) -------------------------------

    def recover_stale_preflight(self, lane_id: str, *, role: str = "claude") -> CliResult:
        """Seed a stale worker in ``lane_id`` and run the public read-only recover-stale preflight."""
        name = self.seed_stale_worker(lane_id, role=role)
        issue = lane_id.split("_")[1] if "_" in lane_id else ""
        return self.recover_stale_execute(
            issue=issue, lane=lane_id, role=role, provider=role,
            assigned_name=name, locator=self.locator_of(name), execute=False,
        )

    def recover_stale_execute(
        self,
        *,
        issue: str,
        lane: str,
        role: str,
        provider: str,
        assigned_name: str,
        locator: str,
        execute: bool = True,
    ) -> CliResult:
        """Dispatch ``sublane recover-stale`` (``--execute`` unless ``execute=False``)."""
        argv = [
            "sublane", "recover-stale",
            "--issue", issue, "--lane", lane, "--role", role, "--provider", provider,
            "--assigned-name", assigned_name, "--locator", locator,
            "--json", "--repo", str(self.repo_root),
        ]
        if execute:
            argv.append("--execute")
        return self.run_cli(argv)

    def session_rollback_cli(self, action_id: str, *, execute: bool = False) -> CliResult:
        """Dispatch ``herdr session-rollback --action-id`` (preflight, or ``--execute``)."""
        argv = ["herdr", "session-rollback", "--action-id", action_id, "--json", "--repo", str(self.repo_root)]
        if execute:
            argv.append("--execute")
        return self.run_cli(argv)

    # -- inventory snapshots (cleanup / residue assertions) -------------------

    def live_locator_count(self) -> int:
        """How many managed agent rows the fake currently reports (a retire-residue probe)."""
        return len(self.fake.agents)
