"""Regression pins for Redmine #14457 — the live sublane adapter must own its runner default.

Measured on the installed TestPyPI ``mozyo-bridge==0.13.0a1`` public CLI (j#87901): a fresh
``sublane create --execute`` reported ``status=ready`` on the dry-run and then died with
``TypeError: 'NoneType' object is not callable`` inside the *pre-mutation* launcher
compatibility gate:

    HerdrSublaneActuatorOps.preflight_launcher_compatibility
      -> evaluate_launcher_compatibility
        -> measure_config_parse_compatibility
          -> runner(...)

``HerdrSublaneActuatorOps.runner`` is the tests' injection seam and is therefore ``None`` in
every production run. The session-start path had always resolved it (``prepare_session`` does
``runner = runner or subprocess.run``), so the skew was invisible until #14258 added the
pre-worktree gate — a second consumer, typed ``Runner`` (non-Optional), that *calls* what it
is handed and was given ``self.runner`` unresolved.

Why every existing pin missed it, and what these add:

- ``PreWorktreeGateTest`` (``test_issue_14258_launcher_target_compat.py``) drives the
  ``--execute`` path with a **fake** ops whose ``preflight_launcher_compatibility`` returns a
  canned verdict tuple. It proves the gate's *placement* (before the worktree) and never
  constructs the real adapter, so the production ``runner=None`` was never on that path.
- ``ConjunctionZeroActuationTest`` calls the conjunction directly and passes
  ``subprocess.run`` **explicitly**, so it exercises the measurement with a runner that was
  never absent.

The gap was the join of the two: the real adapter, uninjected, on the ``--execute`` gate. The
pins below close it from both ends — an admit and a typed refusal reached through the real
adapter with no runner injected, and the whole production use case refusing with zero
mutation instead of raising.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    build_attest_capability_epilog,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    ATTEST_CAPABILITY_MARKER,
    MOZYO_BRIDGE_LAUNCHER_ENV,
)

_V2_CONFIG = """version: 2
agents:
  profiles:
    implementation:
      provider: claude
"""

#: A launcher that satisfies every conjunct (#14258's ``_capable_help`` shape): the marker
#: subcommand plus THIS build's advertised contract, composed by the canonical producer so
#: the fixture can never drift from what the join requires.
def _capable_help() -> str:
    return f"usage: x [{ATTEST_CAPABILITY_MARKER} NAME]\n\n{build_attest_capability_epilog()}\n"


#: Everything the PRE-#14258 conjuncts wanted and nothing #14258 added — the shape that used
#: to launch a pair and now must be refused with a typed reason.
def _pre_14258_help() -> str:
    return (
        f"usage: x [{ATTEST_CAPABILITY_MARKER} NAME]\n"
        "mozyo_attest_capability_schema=2\n"
        "mozyo_attest_capability_stores=1_2\n"
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed_repo(root: Path) -> Path:
    repo = root / "primary"
    (repo / ".mozyo-bridge").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / ".mozyo-bridge" / "config.yaml").write_text(_V2_CONFIG, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    return repo


def _launcher(directory: Path, help_text: str, name: str = "mozyo-bridge") -> str:
    """A REAL executable: the conjunction runs a real subprocess probe.

    A fake runner would defeat the whole point of these pins — the defect *is* the absence of
    a runner, so the pins must let the adapter resolve and run one for itself.
    """
    path = directory / name
    body = "".join(f"printf '%s\\n' {line!r}\n" for line in help_text.splitlines())
    path.write_text("#!/bin/sh\n" + body + "exit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class InstalledSublaneRunnerDefaultTest(unittest.TestCase):
    """The real adapter, with NO runner injected — the only shape production ever has."""

    def _fixture(self, root: Path, help_text: str) -> tuple[Path, dict, str]:
        repo = _seed_repo(root)
        home = root / "home"
        home.mkdir()
        LaneLifecycleStore(home=home).ensure_schema()
        launcher = _launcher(root, help_text)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "MOZYO_BRIDGE_HOME": str(home),
            MOZYO_BRIDGE_LAUNCHER_ENV: launcher,
        }
        return repo, env, _git(repo, "rev-parse", "HEAD")

    def _ops(self, repo: Path, env: dict, **kwargs) -> HerdrSublaneActuatorOps:
        # `runner` is deliberately absent: that IS the production shape and the defect.
        return HerdrSublaneActuatorOps(
            repo_root=repo,
            lane_label="issue_14457_probe_lane",
            issue="14457",
            branch="issue_14457_probe_lane",
            env=env,
            # The #13705 front door is a different gate with its own pins; hold it open so a
            # refusal here can only be the launcher conjunction under test (a gate that
            # blocked for the wrong reason would make these pins vacuous).
            runtime_fingerprint_reader=lambda: {},
            **kwargs,
        )

    def test_a_capable_launcher_is_admitted_with_no_runner_injected(self) -> None:
        # The admit half. It is the one that proves a runner was actually RESOLVED AND RUN:
        # a refusal could also come from an exception being swallowed somewhere, but an
        # admit can only be produced by the launcher's own probes having really answered.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, env, head = self._fixture(root, _capable_help())
            ops = self._ops(repo, env)
            self.assertIsNone(ops.runner, "production never injects a runner")

            ok, reason, detail = ops.preflight_launcher_compatibility(
                base_commit=head,
                lane_runtime_root=str(root / "lane-not-yet-created"),
                from_base_ref=True,
            )

            self.assertTrue(ok, detail)
            self.assertEqual(reason, "")

    def test_an_incompatible_launcher_yields_a_typed_verdict_not_a_crash(self) -> None:
        # The refusal half: pre-fix this raised `TypeError: 'NoneType' object is not
        # callable` out of `measure_config_parse_compatibility`. A crash is not a verdict.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, env, head = self._fixture(root, _pre_14258_help())
            ops = self._ops(repo, env)

            ok, reason, detail = ops.preflight_launcher_compatibility(
                base_commit=head,
                lane_runtime_root=str(root / "lane-not-yet-created"),
                from_base_ref=True,
            )

            self.assertFalse(ok)
            self.assertTrue(reason, "a refusal must carry a typed reason token")
            self.assertTrue(detail)

    def test_an_injected_fake_runner_is_still_the_one_that_is_used(self) -> None:
        # The seam must survive the default: every existing suite drives this adapter through
        # an injected fake herdr, so a default that overrode the injection would be a far
        # larger regression than the one being fixed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, env, _head = self._fixture(root, _capable_help())
            calls: list[list[str]] = []

            def fake_runner(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            ops = self._ops(repo, env, runner=fake_runner)
            self.assertIs(ops._resolve_runner(), fake_runner)

            ops.preflight_launcher_compatibility(
                base_commit=_git(repo, "rev-parse", "HEAD"),
                lane_runtime_root=str(root / "lane-not-yet-created"),
                from_base_ref=True,
            )
            self.assertTrue(calls, "the injected fake runner must receive the probes")

    def test_the_uninjected_default_is_the_approved_subprocess_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, env, _head = self._fixture(root, _capable_help())
            self.assertIs(self._ops(repo, env)._resolve_runner(), subprocess.run)


class InstalledSublaneExecutePathTest(unittest.TestCase):
    """The production ``--execute`` pre-mutation path over the REAL adapter (j#87901 item 2).

    ``--dry-run`` reported ``status=ready`` and never touched the gate, which is exactly why
    the defect shipped: only the execute path reaches ``pre_mutation_admission``. So the pin
    is taken there, over the real adapter with no runner injected, and it asserts the *typed
    reason* rather than merely "blocked" — an earlier gate refusing for its own reason would
    otherwise look identical and pin nothing.
    """

    def test_the_execute_path_refuses_with_a_typed_reason_and_zero_mutation(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = InstalledSublaneRunnerDefaultTest()
            repo, env, _head = fixture._fixture(root, _pre_14258_help())
            ops = fixture._ops(repo, env)
            # #15152 R2 (j#106834): the sender-attestation gate now covers
            # create-only runs and would refuse first in this credential-less
            # fixture; stub it attested so the launcher-compatibility gate stays
            # the one under test.
            ops.preflight_dispatch_sender = lambda: (True, "sender_attested")
            worktree = root / "lane-worktree"

            outcome = SublaneActuateUseCase(ops, gateway_ready_probes=0).run(
                SublaneCreateRequest(
                    issue="14457",
                    lane_label="issue_14457_probe_lane",
                    branch="issue_14457_probe_lane",
                    worktree_path=str(worktree),
                    journal="87901",
                    base_ref="main",
                ),
                execute=True,
                dispatch=False,
                target_repo=str(repo),
            )

            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                "launcher_runtime_incompatible",
                outcome.blocked_reasons,
                f"refused for the wrong reason: {outcome.blocked_reasons}",
            )
            # Zero mutation: the gate runs before `git worktree add`, so neither the
            # directory nor the branch may exist, and no pane may have been observed.
            self.assertFalse(worktree.exists(), "the refusal created a worktree")
            self.assertNotIn(
                "issue_14457_probe_lane",
                _git(repo, "branch", "--list", "issue_14457_probe_lane"),
            )
            self.assertIsNone(outcome.startup)
            self.assertIsNone(outcome.gateway_pane)
            self.assertIsNone(outcome.worker_pane)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
