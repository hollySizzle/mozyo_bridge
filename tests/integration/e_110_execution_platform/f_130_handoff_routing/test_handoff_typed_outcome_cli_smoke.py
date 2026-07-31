"""High-level handoff CLI contract smoke: typed outcome, never a traceback (Redmine #14232).

Issue acceptance 3: *do not expose a stack trace as the normal CLI contract; reconcile the exit
code with a structured outcome.* Every other test in this lane drives Python objects. This one
drives the **console-script entry point in a subprocess**, which is the only place that claim can
actually be checked: the defect's user-visible shape was an uncaught traceback on stderr and an
exit 1 with nothing structured on stdout, and no in-process assertion sees that.

Two tiers, deliberately separated because they answer different questions:

1. :class:`InstalledArtifactHandoffCliSmokeTest` — the **exact-installed** tier (acceptance 5).
   It stages this worktree's packaging inputs into a temp dir, ``pip install``s that into a
   throwaway venv, and runs *that venv's* ``mozyo-bridge`` console script, so what is exercised is
   the installed artifact rather than the source tree — including that the artifact actually ships the new authority module, which a
   packaging omission would otherwise hide. It reuses the nested-install hermeticity precedent
   (``tests/regressions/test_issue_13733_shard_env_hermetic.py`` / :func:`hermetic_python_env`),
   and skips with an explicit reason only when the venv / pip / PEP 517 build backend is
   unavailable.
2. :class:`SourceEntryPointHandoffCliSmokeTest` — the always-runnable tier. It runs the same
   ``mozyo_bridge.application.cli`` entry point in a subprocess against this worktree, so the CLI
   contract is verified on every run even where tier 1 skips.

Both tiers exercise the handoff boundary on **paths that need no live receiver**, so they are
deterministic and touch no pane: the front door's own anchor fail-close (which returns before any
rail is resolved) and a send-semantics refusal on the anchored send rail. The transport-failure path
itself needs a real attested workspace plus a fake herdr binary, which is live-lane / dogfood
territory this lane does not enter; it is covered deterministically at the shim + rail composition
in ``test_handoff_fake_herdr_transport_failure.py`` instead.

Note on parity limits (recorded rather than papered over): the operator's ambient
``mozyo-bridge`` on ``PATH`` is a pipx install of a *released* build, so running it would test a
different artifact than this worktree. Tier 1 therefore installs this worktree's own staged
source explicitly. Parity of the ambient installed launcher requires a release, which this lane does not
perform.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv as _venv
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from tests.support.nested_python import hermetic_python_env  # noqa: E402

#: A handoff invocation that fails closed in the q-enter front door *before* any rail is
#: resolved (no anchor for an anchored intent), so it needs no tmux, no herdr, and no receiver.
_FRONT_DOOR_ANCHOR_REFUSAL = [
    "handoff", "q-enter",
    "--intent", "worker_dispatch",
    "--to", "claude",
    "--record-format", "json",
]

#: A handoff invocation the anchored send rail refuses through the shared send-semantics
#: authority (``queue-enter`` rejects ``--force``). Chosen deliberately over an argparse-level
#: refusal such as an invalid ``--kind`` choice: argparse exits 2 with a usage message and never
#: enters ``orchestrate_handoff``, so it proves nothing about the *delivery outcome* contract.
#: This one reaches the rail's ``_emit`` and returns a real ``DeliveryOutcome`` — while still
#: needing no receiver, since it refuses before any target is resolved and nothing is typed.
_SEND_SEMANTICS_REFUSAL = [
    "handoff", "send",
    "--to", "claude",
    "--source", "redmine",
    "--issue", "14232",
    "--journal", "94407",
    "--kind", "reply",
    "--mode", "queue-enter",
    "--force",
    "--record-format", "json",
]

#: ``orchestrate_handoff`` calls ``require_tmux()`` before its first typed terminal, and that
#: check is exactly "is the ``tmux`` binary on PATH" (no server needed). The send-outcome tier
#: therefore skips rather than fails on a host without tmux installed.
_TMUX_AVAILABLE = shutil.which("tmux") is not None


def _json_objects(stdout: str) -> "list[dict]":
    """Every single-line JSON object in ``stdout`` (the CLI's structured record channel)."""
    found = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                found.append(obj)
    return found


class _HandoffCliContractAssertions:
    """The shared contract both tiers assert about a refused high-level handoff invocation."""

    def assert_typed_refusal(self, proc: "subprocess.CompletedProcess[str]") -> None:
        # (a) the exit code says it failed ...
        self.assertNotEqual(
            proc.returncode, 0, msg=f"expected a non-zero exit; stdout={proc.stdout[:400]}"
        )
        # (b) ... and stderr carries no Python traceback. This is the #14232 CLI contract: a
        # refusal is a structured outcome, not an uncaught exception.
        self.assertNotIn("Traceback (most recent call last)", proc.stderr)
        self.assertNotIn("Traceback (most recent call last)", proc.stdout)
        # (c) ... and a structured record was printed for the caller to branch on.
        objects = _json_objects(proc.stdout)
        self.assertTrue(
            objects,
            msg=(
                "no structured JSON record was printed; the exit code alone is not a "
                f"classifiable outcome. stdout={proc.stdout[:400]}"
            ),
        )

    def assert_front_door_refusal(self, proc: "subprocess.CompletedProcess[str]") -> None:
        self.assert_typed_refusal(proc)
        envelope = next(
            (o for o in _json_objects(proc.stdout) if o.get("q_enter")), None
        )
        self.assertIsNotNone(envelope, "no q-enter front-door envelope was printed")
        self.assertTrue(envelope["blocked"])
        self.assertFalse(envelope["dispatched"])
        # Redmine #14232: the front door's own fail-close resolved no rail, so it must not claim
        # one — and it must not carry an injection stage, because nothing was attempted.
        self.assertFalse(envelope["resolved"])
        self.assertIsNone(envelope["injection_stage"])
        self.assertFalse(envelope["blind_retry_prohibited"])

    def assert_send_refusal_carries_injection_stage(
        self, proc: "subprocess.CompletedProcess[str]"
    ) -> None:
        self.assert_typed_refusal(proc)
        outcome = next(
            (
                o
                for o in _json_objects(proc.stdout)
                if "status" in o and "reason" in o
            ),
            None,
        )
        self.assertIsNotNone(outcome, "no delivery outcome was printed")
        self.assertEqual(outcome["status"], "blocked")
        # Redmine #14232: EVERY delivery outcome carries the injection-stage projection, so a
        # caller never has to re-derive retry safety. A pre-injection refusal is `not_sent`.
        self.assertEqual(outcome["injection_stage"]["stage"], "not_sent")
        self.assertFalse(outcome["injection_stage"]["blind_retry_prohibited"])
        self.assertTrue(outcome["injection_stage"]["next_action"])


class SourceEntryPointHandoffCliSmokeTest(
    unittest.TestCase, _HandoffCliContractAssertions
):
    """Tier 2: the real CLI entry point in a subprocess against this worktree (always runs)."""

    def _run(self, argv: "list[str]") -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        return subprocess.run(
            [sys.executable, "-c", "from mozyo_bridge.application.cli import main; raise SystemExit(main())", *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_front_door_anchor_refusal_is_typed(self) -> None:
        self.assert_front_door_refusal(self._run(_FRONT_DOOR_ANCHOR_REFUSAL))

    @unittest.skipUnless(_TMUX_AVAILABLE, "tmux is not on PATH; require_tmux() refuses first")
    def test_send_semantics_refusal_carries_the_injection_stage(self) -> None:
        self.assert_send_refusal_carries_injection_stage(self._run(_SEND_SEMANTICS_REFUSAL))


class InstalledArtifactHandoffCliSmokeTest(
    unittest.TestCase, _HandoffCliContractAssertions
):
    """Tier 1: the exact-installed artifact's console script (acceptance 5).

    Installed once per class (the install is the expensive step; all three cases are read-only
    against the same venv). ``pip install <worktree>`` is the build frontend rather than
    ``python -m build`` + a wheel install: both produce the same PEP 517 artifact from this
    ``pyproject.toml``, and ``build`` is not importable in every environment the suite runs in
    (the ``test_issue_13733`` precedent skips for exactly that reason). Going through pip keeps
    this tier *executing* instead of skipping, which is the whole point of an installed-artifact
    smoke.
    """

    venv_script: "Path | None" = None
    skip_reason: str = ""

    #: The packaging inputs this project's static-version ``pyproject.toml`` needs. Copied into
    #: the temp dir so the build runs OUT OF TREE — see :meth:`_staged_source`.
    _PACKAGING_INPUTS = ("pyproject.toml", "README.md", "LICENSE")

    @classmethod
    def _staged_source(cls, tmp_path: Path) -> Path:
        """Copy the packaging inputs into ``tmp_path`` and return that staged source root.

        A ``pip install <worktree>`` builds **in place**: setuptools writes ``build/`` and
        ``src/mozyo_bridge.egg-info/`` into the worktree. Those artifacts are not merely untidy —
        a stale / newly-created ``egg-info`` changes what a *later* test in the same suite run
        observes about the installed distribution, which is the #13733 / #13735 family of
        cross-test pollution this repo has already been bitten by (measured here: installing in
        place turned two unrelated installed-launcher regressions red in the same full-suite run).
        Staging the inputs keeps the build entirely inside the temp dir, so the worktree this
        suite is measuring is never mutated by measuring it.
        """
        staged = tmp_path / "src_tree"
        staged.mkdir()
        for name in cls._PACKAGING_INPUTS:
            shutil.copy2(ROOT / name, staged / name)
        shutil.copytree(
            ROOT / "src",
            staged / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
        )
        return staged

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls._tmp.name)
        venv_dir = tmp_path / "venv"
        try:
            _venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        except (subprocess.CalledProcessError, OSError) as exc:
            cls.skip_reason = f"venv with pip could not be created: {exc}"
            return
        try:
            staged = cls._staged_source(tmp_path)
        except OSError as exc:
            cls.skip_reason = f"packaging inputs could not be staged: {exc}"
            return
        # The nested install must not see this worktree's `src/` — it carries an egg-info that pip
        # resolves as already-installed, so pip exits 0 having installed nothing and written no
        # console script (#13733 / #13735 F1). `hermetic_python_env` strips that inheritance.
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src")}):
            nested_env = hermetic_python_env()
        install = subprocess.run(
            [
                str(venv_dir / "bin" / "python"), "-m", "pip", "install",
                "-q", "--disable-pip-version-check", str(staged),
            ],
            capture_output=True,
            text=True,
            env=nested_env,
        )
        if install.returncode != 0:
            cls.skip_reason = (
                "pip install of the staged source failed (no network for the PEP 517 build "
                f"backend, or pip unavailable); stderr={install.stderr[:300]}"
            )
            return
        script = venv_dir / "bin" / "mozyo-bridge"
        if not script.exists():
            cls.skip_reason = (
                "pip exited 0 but wrote no console script — it resolved mozyo-bridge as already "
                "installed from an inherited PYTHONPATH and skipped the install (#13735 F1)"
            )
            return
        cls.venv_script = script
        cls.nested_env = nested_env

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        if self.venv_script is None:
            self.skipTest(self.skip_reason or "installed artifact unavailable")

    def _run(self, argv: "list[str]") -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [str(self.venv_script), *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.nested_env,
        )

    def test_installed_console_script_front_door_refusal_is_typed(self) -> None:
        self.assert_front_door_refusal(self._run(_FRONT_DOOR_ANCHOR_REFUSAL))

    @unittest.skipUnless(_TMUX_AVAILABLE, "tmux is not on PATH; require_tmux() refuses first")
    def test_installed_console_script_send_refusal_carries_the_injection_stage(self) -> None:
        self.assert_send_refusal_carries_injection_stage(self._run(_SEND_SEMANTICS_REFUSAL))

    def test_installed_artifact_ships_the_injection_stage_authority(self) -> None:
        """The wheel must actually carry the new module — a packaging omission is silent."""
        proc = subprocess.run(
            [
                str(self.venv_script.parent / "python"),
                "-c",
                "from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain."
                "injection_stage import INJECTION_STAGES; print(sorted(INJECTION_STAGES))",
            ],
            capture_output=True,
            text=True,
            env=self.nested_env,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[:400])
        self.assertIn("submitted_confirmed", proc.stdout)


if __name__ == "__main__":  # pragma: no cover - manual runner parity
    unittest.main()
