"""Regression pins for Redmine #14258 — launcher vs TARGET authority compatibility.

The #13748 / #13847 / #13882 preflights all verify the selected managed-launch launcher
against the *attestation store*: it carries ``herdr agent-attest``, advertises the required
schema, and can write the store shape on disk. None of them asks whether the launcher can
**read** the two authorities the lane will point it at, and a skew in either kills the lane
*after* it has been created:

- the target repo's ``.mozyo-bridge/config.yaml`` — the wrapper starts with
  ``--cwd <lane worktree>`` and a mozyo-bridge CLI parses that config at startup. Measured
  (j#85834): the installed launcher passed the ``agent-attest`` capability probe, then read
  the lane's v2 config, reported ``unknown key 'agents'``, and exited 2. ``sublane create
  --execute`` had already created the worktree; both slots came up ``provider_exited /
  rollback_owed``.
- the home-scoped **shared** lane lifecycle authority — additively migrated by whichever
  lane's source CLI is newest. Measured (j#85890): a launcher that could read the v2 config
  still zero-started the named lane against the v7 store with
  ``LaneLifecycleReaderUpgradeRequired``.

What #14258 adds, and what these pins characterize:

1. both capabilities are **advertised** (``mozyo_attest_capability_config`` /
   ``_config_keys`` / ``_lifecycle``) and joined against the real target, so the answer is a
   declaration rather than #14231's incidental "the launcher happens to exit non-zero in that
   cwd" — which is what makes it answerable *before* the lane worktree exists;
2. the join fails closed on every unprovable axis (no advertisement, an unreadable /
   unsupported target) and admits the compatible case;
3. the whole conjunction runs at the ``sublane create`` pre-worktree gate, so an incompatible
   launcher leaves **no worktree, no pane, no rollback debt**;
4. an explicit ``MOZYO_BRIDGE_LAUNCHER`` override is subject to the same join — a scoped
   launcher is the documented recovery, not a bypass;
5. the #13748 command-capability contract still rejects what it always rejected.

Characterization only; the fix lives in ``herdr_launcher_capability.py`` (the pure joins),
``herdr_pane_lifecycle.py`` (the conjunction), ``repo_local_config_loader.py`` /
``lane_lifecycle_readonly.py`` (the read-only probes), and
``sublane_actuator_herdr_preflight.py`` + ``sublane_actuator_gates.py`` (the pre-worktree gate).
"""

from __future__ import annotations

import os
import re
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
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    LIFECYCLE_SCHEMA_ABSENT,
    LIFECYCLE_SCHEMA_RECOGNIZED,
    LIFECYCLE_SCHEMA_UNREADABLE,
    probe_lane_lifecycle_schema,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    LANE_LIFECYCLE_SCHEMA_VERSION,
    lane_lifecycle_path,
    readable_lane_lifecycle_versions,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    CONFIG_PARSE_CONTRACT_VERSION,
    LAUNCHER_CANNOT_PARSE_TARGET_CONFIG,
    LAUNCHER_CANNOT_READ_LIFECYCLE,
    LAUNCHER_CONFIG_VALIDATOR_ABSENT,
    TARGET_CONFIG_INVALID,
    LAUNCHER_LIFECYCLE_CONTRACT_ABSENT,
    LIFECYCLE_JOIN_OK,
    TARGET_SCHEMA_ABSENT,
    TARGET_SCHEMA_DECLARED,
    TargetSchemaObservation,
    build_attest_capability_epilog,
    decide_lifecycle_reader_compatibility,
    parse_launcher_capability_output,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    REPO_SELECTION_ENV_VARS,
    HerdrLauncherIncompatibleError,
    preflight_launcher_compatibility,
    repo_neutral_env,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (  # noqa: E501
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
)

from tests.support.private_path_fixtures import macos_home_path

_MARKER = "--assigned-name"

#: The v2 config that reproduced the measured failure: the top-level `agents` topology an
#: older launcher's parser rejects as an unknown key.
_V2_CONFIG = """version: 2
agents:
  profiles:
    implementation:
      provider: claude
"""

_TIMEOUT = 10.0


def _capable_help() -> str:
    """What THIS build's launcher advertises — the canonical contract, never a copy."""
    return f"usage: x [{_MARKER} NAME]\n\n{build_attest_capability_epilog()}\n"


def _observation(help_text: str):
    return parse_launcher_capability_output(help_text)


class LifecycleSchemaProbeTest(unittest.TestCase):
    """The shared lane lifecycle authority is probed read-only and never migrated."""

    def test_absent_store_is_absent_and_a_real_store_reports_its_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(
                probe_lane_lifecycle_schema(home=home).state, LIFECYCLE_SCHEMA_ABSENT
            )
            LaneLifecycleStore(home=home).records()  # a read creates nothing
            self.assertEqual(
                probe_lane_lifecycle_schema(home=home).state, LIFECYCLE_SCHEMA_ABSENT
            )
            LaneLifecycleStore(home=home).ensure_schema()
            probe = probe_lane_lifecycle_schema(home=home)
            self.assertEqual(probe.state, LIFECYCLE_SCHEMA_RECOGNIZED)
            self.assertEqual(probe.version, LANE_LIFECYCLE_SCHEMA_VERSION)

    def test_the_probe_does_not_migrate_or_touch_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            LaneLifecycleStore(home=home).ensure_schema()
            path = lane_lifecycle_path(home)
            before = path.read_bytes()
            probe_lane_lifecycle_schema(home=home)
            self.assertEqual(path.read_bytes(), before)

    def test_a_corrupt_store_is_unreadable_not_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = lane_lifecycle_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"this is not a sqlite database")
            self.assertEqual(
                probe_lane_lifecycle_schema(home=home).state,
                LIFECYCLE_SCHEMA_UNREADABLE,
            )


class LifecycleJoinTest(unittest.TestCase):
    """The pure lifecycle join: READ capability against the shape actually on disk."""

    def test_this_builds_launcher_reads_this_builds_lifecycle_shape(self) -> None:
        verdict = decide_lifecycle_reader_compatibility(
            _observation(_capable_help()),
            TargetSchemaObservation(
                TARGET_SCHEMA_DECLARED, LANE_LIFECYCLE_SCHEMA_VERSION
            ),
        )
        self.assertTrue(verdict.ok, verdict.detail)
        self.assertEqual(verdict.reason, LIFECYCLE_JOIN_OK)

    def test_an_absent_shared_authority_admits_the_launch(self) -> None:
        verdict = decide_lifecycle_reader_compatibility(
            _observation(f"usage: x [{_MARKER} NAME]\n"),
            TargetSchemaObservation(TARGET_SCHEMA_ABSENT),
        )
        self.assertTrue(verdict.ok, verdict.detail)

    def test_the_measured_v6_reader_vs_v7_store_skew(self) -> None:
        # j#85890: a launcher that COULD read the v2 config still zero-started the lane,
        # because its lifecycle reader predated the shared store's shape.
        v6_reader = (
            f"usage: x [{_MARKER} NAME]\n"
            "mozyo_attest_capability_lifecycle=1_2_3_4_5_6\n"
        )
        verdict = decide_lifecycle_reader_compatibility(
            _observation(v6_reader), TargetSchemaObservation(TARGET_SCHEMA_DECLARED, 7)
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LAUNCHER_CANNOT_READ_LIFECYCLE)

    def test_a_launcher_advertising_no_reader_capability_fails_closed(self) -> None:
        verdict = decide_lifecycle_reader_compatibility(
            _observation(f"usage: x [{_MARKER} NAME]\n"),
            TargetSchemaObservation(TARGET_SCHEMA_DECLARED, 7),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LAUNCHER_LIFECYCLE_CONTRACT_ABSENT)

    def test_this_build_advertises_exactly_what_its_reader_recognizes(self) -> None:
        # The advertisement is derived from the reader's own constant, so a schema bump
        # cannot leave the advertised capability behind (which would make this build refuse
        # its own launcher).
        self.assertEqual(
            _observation(_capable_help()).advertised_lifecycle_versions,
            readable_lane_lifecycle_versions(),
        )


class ConjunctionZeroActuationTest(unittest.TestCase):
    """The whole conjunction refuses before anything is created — and admits the good case."""

    def _launcher(self, directory: Path, body: str) -> str:
        path = directory / "mozyo-bridge"
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    def _script(self, help_text: str) -> str:
        # A real executable: the conjunction runs a real subprocess probe, so a fake runner
        # would not prove the launcher path is exercised end to end.
        lines = "".join(
            f"printf '%s\\n' {line!r}\n" for line in help_text.splitlines()
        )
        return "#!/bin/sh\n" + lines + "exit 0\n"

    def test_a_compatible_launcher_passes_the_whole_conjunction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                _V2_CONFIG, encoding="utf-8"
            )
            LaneLifecycleStore(home=root / "home").ensure_schema()
            launcher = self._launcher(root, self._script(_capable_help()))
            observation = preflight_launcher_compatibility(
                launcher,
                subprocess.run,
                _TIMEOUT,
                dict(os.environ),
                repo_root=root / "repo",
                store_home=root / "home",
            )
            self.assertTrue(observation.subcommand_marker_present)

    def test_an_incompatible_launcher_is_refused_with_the_typed_reason(self) -> None:
        # A launcher that satisfies every PRE-#14258 conjunct (subcommand marker, attestation
        # schema, writable stores) and nothing else: before this issue it launched the pair.
        pre_14258 = (
            f"usage: x [{_MARKER} NAME]\n"
            "mozyo_attest_capability_schema=2\n"
            "mozyo_attest_capability_stores=1_2\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                _V2_CONFIG, encoding="utf-8"
            )
            launcher = self._launcher(root, self._script(pre_14258))
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher,
                    subprocess.run,
                    _TIMEOUT,
                    dict(os.environ),
                    repo_root=root / "repo",
                    store_home=root / "home",
                )
            self.assertEqual(caught.exception.reason, LAUNCHER_CONFIG_VALIDATOR_ABSENT)
            # The refusal must be actionable and must not persist a private absolute path.
            message = str(caught.exception)
            self.assertIn("MOZYO_BRIDGE_LAUNCHER", message)
            self.assertIn("No workspace / tab / agent was created", message)

    def test_the_refusal_names_the_recovery_not_the_operators_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = self._launcher(
                root, self._script(f"usage: x [{_MARKER} NAME]\n")
            )
            with self.assertRaises((HerdrLauncherIncompatibleError, HerdrSessionStartError)):
                preflight_launcher_compatibility(
                    launcher,
                    subprocess.run,
                    _TIMEOUT,
                    dict(os.environ),
                    repo_root=root,
                    store_home=root / "home",
                )

    def test_the_conjunction_runs_every_conjunct(self) -> None:
        # GUARD BITE: the joins are only worth anything if the conjunction calls them. A
        # refactor that dropped one would otherwise pass every test above, which is exactly
        # how the target-authority conjuncts came to be missing in the first place.
        import inspect

        source = inspect.getsource(preflight_launcher_compatibility)
        for conjunct in (
            "preflight_attest_launcher_capability",
            "preflight_attest_store_schema",
            "preflight_launcher_target_authorities",
        ):
            self.assertIn(conjunct, source, f"{conjunct} must be part of the conjunction")


class PreWorktreeGateTest(unittest.TestCase):
    """Close condition 1: refuse before the first PROCESS **or WORKTREE** mutation."""

    class _Ops:
        """The minimal creation-side port the use case drives, with nothing implemented.

        Every mutation raises: the point of the pins below is that none of them is reached.
        """

        def __init__(self, verdict):
            self.verdict = verdict
            self.calls: list[str] = []

        def is_git_workspace(self) -> bool:
            return True

        def worktree_exists(self, branch: str) -> bool:
            return False

        def preflight_launcher_compatibility(self, **kwargs):
            self.calls.append(f"preflight:{sorted(kwargs)}")
            return self.verdict

        def create_worktree(self, **kwargs) -> None:
            raise AssertionError("the gate should have refused before the worktree")

        def append_lane_column(self, worktree_path: str):
            raise AssertionError("the gate should have refused before any pane")

        def append_lane_argv(self, worktree_path: str) -> list[str]:
            return []

        def read_lane(self, worktree_path: str):
            raise AssertionError("the gate should have refused before any read-back")

        def declare_adopted_lane_lifecycle(self, worktree_path: str, *, adopted: bool):
            raise AssertionError("no lifecycle write before the gate passes")

        def probe_gateway_ready(self, gateway_pane: str) -> bool:
            return False

        def dispatch_implementation_request(self, **kwargs) -> int:
            raise AssertionError("no dispatch before the gate passes")

    def _run(self, verdict):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        ops = self._Ops(verdict)
        request = SublaneCreateRequest(
            issue="14258",
            lane_label="issue_14258_lane",
            branch="issue_14258_lane",
            worktree_path="/tmp/does-not-exist-14258",
            journal="87708",
            base_ref="origin/main-next",
        )
        outcome = SublaneActuateUseCase(ops, gateway_ready_probes=0).run(
            request, execute=True, dispatch=False, target_repo="/tmp/does-not-exist-14258"
        )
        return ops, outcome

    def test_an_incompatible_launcher_creates_no_worktree_and_owes_no_rollback(self) -> None:
        ops, outcome = self._run(
            (False, LAUNCHER_CANNOT_PARSE_TARGET_CONFIG, "launcher rejects the lane config")
        )
        self.assertTrue(outcome.is_blocked)
        self.assertIn("launcher_runtime_incompatible", outcome.blocked_reasons)
        self.assertIn(LAUNCHER_CANNOT_PARSE_TARGET_CONFIG, outcome.blocked_reasons)
        self.assertIsNone(outcome.startup, "a zero-mutation refusal observed no startup")
        self.assertIsNone(outcome.gateway_pane)
        self.assertIsNone(outcome.worker_pane)
        self.assertEqual(len(ops.calls), 1, "the gate runs exactly once, before the worktree")

    def test_the_gate_is_told_which_config_the_lane_will_have(self) -> None:
        # A worktree this run would CREATE has no directory to read, so the gate must ask for
        # the committed blob at the lane's base ref rather than substituting this checkout's
        # working file — the proxy that would silently pass on a differing base.
        ops, _ = self._run((False, LAUNCHER_CANNOT_PARSE_TARGET_CONFIG, "nope"))
        self.assertEqual(
            ops.calls,
            ["preflight:['base_commit', 'from_base_ref', 'lane_runtime_root']"],
        )

    def test_a_port_without_the_capability_is_a_no_op(self) -> None:
        # The tmux adapter and every existing test fake omit the optional capability; the
        # gate must not start requiring it (byte-invariant for those ports).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_gates import (  # noqa: E501
            launcher_compatibility_gate,
        )

        class _NoCapability:
            pass

        self.assertIsNone(
            launcher_compatibility_gate(
                type("_U", (), {"ops": _NoCapability()})(),
                object(),
                launch_action="create_worktree",
                dispatch=False,
                fill_decision=None,
                fill_override_reason=None,
            )
        )


class ExplicitLauncherOverrideTest(unittest.TestCase):
    """Close condition: an explicit override is a scoped recovery, never a bypass."""

    def _launcher(self, directory: Path, help_text: str) -> str:
        path = directory / "scoped-mozyo-bridge"
        lines = "".join(
            f"printf '%s\\n' {line!r}\n" for line in help_text.splitlines()
        )
        path.write_text("#!/bin/sh\n" + lines + "exit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    def test_a_capable_override_is_admitted_and_an_incapable_one_is_not(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            MOZYO_BRIDGE_LAUNCHER_ENV,
            resolve_attest_launcher,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                _V2_CONFIG, encoding="utf-8"
            )
            capable = self._launcher(root / "repo", _capable_help())
            incapable = self._launcher(
                root,
                f"usage: x [{_MARKER} NAME]\n"
                "mozyo_attest_capability_schema=2\n"
                "mozyo_attest_capability_stores=1_2\n",
            )
            for launcher, admitted in ((capable, True), (incapable, False)):
                with self.subTest(admitted=admitted):
                    env = {MOZYO_BRIDGE_LAUNCHER_ENV: launcher}
                    self.assertEqual(resolve_attest_launcher(env), launcher)
                    if admitted:
                        preflight_launcher_compatibility(
                            launcher,
                            subprocess.run,
                            _TIMEOUT,
                            env,
                            repo_root=root / "repo",
                            store_home=root / "home",
                        )
                    else:
                        with self.assertRaises(HerdrLauncherIncompatibleError):
                            preflight_launcher_compatibility(
                                launcher,
                                subprocess.run,
                                _TIMEOUT,
                                env,
                                repo_root=root / "repo",
                                store_home=root / "home",
                            )


class CanonicalContractProducerTest(unittest.TestCase):
    """One producer for the advertised contract, so no producer can fall behind."""

    def test_the_cli_epilog_is_the_canonical_composer(self) -> None:
        import argparse

        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        # Reach the `herdr agent-attest` subparser's rendered help the same way a probe does.
        help_text = _agent_attest_help(parser)
        for token in build_attest_capability_epilog().splitlines():
            if "=" in token:
                self.assertIn(
                    token,
                    help_text,
                    "every advertised token must survive argparse's help rendering intact",
                )
        self.assertIsInstance(parser, argparse.ArgumentParser)

    def test_this_build_advertises_the_contract_it_requires(self) -> None:
        # The self-consistency the join depends on: THIS build's advertisement satisfies
        # THIS build's requirements. Without it the runtime would refuse its own launcher.
        observation = _observation(
            f"usage: x [{_MARKER} NAME]\n{build_attest_capability_epilog()}"
        )
        self.assertEqual(
            observation.advertised_config_parse_contract, CONFIG_PARSE_CONTRACT_VERSION
        )
        lifecycle = decide_lifecycle_reader_compatibility(
            observation,
            TargetSchemaObservation(
                TARGET_SCHEMA_DECLARED, LANE_LIFECYCLE_SCHEMA_VERSION
            ),
        )
        self.assertTrue(lifecycle.ok, lifecycle.detail)


# ---------------------------------------------------------------------------
# Review j#87746 / j#87752 blocking findings — R1 / R2 / R3 / R4.
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)


def _seed_repo(root: Path, config_text: str) -> Path:
    repo = root / "primary"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    cfg = repo / ".mozyo-bridge"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(config_text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    return repo


class R1MovingBaseRefTest(unittest.TestCase):
    """R1: a moving base ref must not let an unverified config into a new worktree."""

    def test_a_string_ref_is_not_a_pin_but_the_resolved_commit_is(self) -> None:
        # The defect, then the fix, on the same repo: `main` advances between the read and
        # the materialization, so only a resolved commit addresses one document.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _seed_repo(root, _V2_CONFIG)
            ops = LiveSublaneGitOperations(repo_root=repo)
            pinned = ops.resolve_commit("main")
            self.assertRegex(pinned, r"^[0-9a-f]{40}$")

            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 99\n", encoding="utf-8"
            )
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "c2")

            # The moving ref now names the NEW commit; the pin still names the old one.
            self.assertNotEqual(ops.resolve_commit("main"), pinned)
            state, text = ops.committed_blob(
                ref=pinned, relpath=".mozyo-bridge/config.yaml"
            )
            self.assertEqual(state, "blob_present")
            self.assertIn("version: 2", text)
            self.assertNotIn("version: 99", text)

    def test_the_pinned_commit_is_what_the_worktree_materializes(self) -> None:
        # GUARD BITE for the actual close condition: the config that was verified is the
        # config that lands. Passing the ref instead of the pin makes this assertion fail.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _seed_repo(root, _V2_CONFIG)
            ops = LiveSublaneGitOperations(repo_root=repo)
            pinned = ops.resolve_commit("main")
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 99\n", encoding="utf-8"
            )
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "c2")

            worktree = root / "lane"
            ops.create_worktree(
                branch="lane_x", worktree_path=str(worktree), base_ref=pinned
            )
            materialized = (worktree / ".mozyo-bridge" / "config.yaml").read_text()
            self.assertIn("version: 2", materialized)
            self.assertNotIn("version: 99", materialized)

    def test_an_unresolvable_or_non_commit_base_yields_no_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _seed_repo(Path(tmp), _V2_CONFIG)
            ops = LiveSublaneGitOperations(repo_root=repo)
            for ref in ("no-such-ref", "refs/heads/nope", ""):
                with self.subTest(ref=ref):
                    self.assertEqual(ops.resolve_commit(ref), "")

    def test_the_gate_refuses_a_lane_whose_base_cannot_be_pinned(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_gates import (  # noqa: E501
            pin_base_commit,
        )

        class _Ops:
            def resolve_base_commit(self, ref):
                return ""

        blocked = {}

        class _UseCase:
            ops = _Ops()

            def _blocked(self, request, **kwargs):
                blocked.update(kwargs)
                return "BLOCKED"

        request, outcome = pin_base_commit(
            _UseCase(),
            type("_R", (), {"base_ref": "origin/main-next"})(),
            launch_action="create_worktree",
            dispatch=False,
            fill_decision=None,
            fill_override_reason=None,
        )
        self.assertEqual(outcome, "BLOCKED")
        self.assertIn("base_ref_unpinnable", blocked["reasons"])


class R2LifecycleProbeMatchesTheReaderTest(unittest.TestCase):
    """R2: the probe must not credit a shape the real reader will refuse."""

    def test_a_broken_column_signature_is_refused_by_probe_and_reader_alike(self) -> None:
        import sqlite3

        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            LIFECYCLE_SCHEMA_UNSUPPORTED,
            LaneLifecycleReader,
        )
        from mozyo_bridge.core.state.lane_lifecycle_schema import TABLE, LaneLifecycleError

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            LaneLifecycleStore(home=home).ensure_schema()
            # Metadata / recorded version / table all stay intact; only the live column
            # signature is broken. `readonly_component_status` still says "recognized",
            # which is exactly why the probe must consult the reader's own authority.
            conn = sqlite3.connect(lane_lifecycle_path(home))
            conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN lane_kind")
            conn.commit()
            conn.close()

            probe = probe_lane_lifecycle_schema(home=home)
            self.assertEqual(probe.state, LIFECYCLE_SCHEMA_UNSUPPORTED)
            self.assertFalse(
                probe.upgrade_required,
                "a broken signature is a partial/corrupt shape, not a stale reader",
            )
            with self.assertRaises(LaneLifecycleError):
                LaneLifecycleReader(home=home).records()

    def test_the_join_refuses_that_store_for_every_launcher(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            LIFECYCLE_UNSUPPORTED,
            TARGET_SCHEMA_UNSUPPORTED,
        )

        verdict = decide_lifecycle_reader_compatibility(
            _observation(_capable_help()),
            TargetSchemaObservation(TARGET_SCHEMA_UNSUPPORTED, 7, upgrade_required=False),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LIFECYCLE_UNSUPPORTED)
        self.assertIn("repair", verdict.detail)


class R3RecoveryHintIsCompleteTest(unittest.TestCase):
    """R3: the public recovery hint must be a complete, actionable instruction."""

    _EXPECTED_HINT = (
        "Recovery: either install / release a mozyo-bridge whose CLI advertises the "
        "required capability, or set `MOZYO_BRIDGE_LAUNCHER` to the absolute path of a "
        "launcher built from a source tree that advertises it."
    )

    def test_both_refusals_carry_the_full_hint_verbatim(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            CONFIG_PARSE_BOTH_OK,
            CONFIG_PARSE_CONTRACT_VERSION as _CV,
            ConfigParseObservation,
            decide_config_parse_compatibility,
        )

        bare = _observation(f"usage: x [{_MARKER} NAME]\n")
        config = decide_config_parse_compatibility(
            bare, ConfigParseObservation(CONFIG_PARSE_BOTH_OK), required_contract_version=_CV
        )
        lifecycle = decide_lifecycle_reader_compatibility(
            bare, TargetSchemaObservation(TARGET_SCHEMA_DECLARED, 7)
        )
        for verdict in (config, lifecycle):
            with self.subTest(reason=verdict.reason):
                self.assertFalse(verdict.ok)
                # The WHOLE sentence, not a substring of it: the defect was a hint that
                # trailed off after "that does", which `assertIn("MOZYO_BRIDGE_LAUNCHER")`
                # happily accepted.
                self.assertTrue(
                    verdict.detail.endswith(self._EXPECTED_HINT),
                    f"detail did not end with the complete hint: {verdict.detail!r}",
                )


class R4NestedGrammarTest(unittest.TestCase):
    """R4: a launcher that cannot parse a NESTED key must be refused, not admitted."""

    #: The exact axis of the counterexample: `lane_placement.by_lane_kind` was added by
    #: commit `d28e59e2` with no change to the config version or the top-level key set.
    _NESTED_CONFIG = (
        "version: 2\n"
        "lane_placement:\n"
        "  version: 1\n"
        "  by_lane_kind:\n"
        "    implementation:\n"
        "      split: right\n"
    )

    def _launcher(self, directory: Path, name: str, body: str) -> str:
        path = directory / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    def _advertising(self, extra_reject: str = "") -> str:
        """A launcher advertising THIS build's exact contract, whose validator may differ."""
        epilog = "".join(
            f"printf '%s\\n' {line!r}\n"
            for line in (f"usage: x [{_MARKER} NAME]", *build_attest_capability_epilog().splitlines())
        )
        return (
            "#!/bin/sh\n"
            'if [ "$1" = "config" ] && [ "$2" = "check-parse" ]; then\n'
            + extra_reject
            + "  exit 0\n"
            "fi\n" + epilog + "exit 0\n"
        )

    def test_this_builds_config_is_valid_here(self) -> None:
        # The premise: the nested key is valid for the CURRENT runtime, so a refusal below
        # can only be about the launcher — never about a broken config.
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config_from_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(self._NESTED_CONFIG, encoding="utf-8")
            self.assertIsNotNone(load_repo_local_config_from_path(path))

    def test_a_launcher_that_rejects_the_nested_key_is_refused(self) -> None:
        # THE R4 regression: identical version + top-level advertisement, different nested
        # grammar. The summary-based join admitted this; the direct measurement refuses it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                self._NESTED_CONFIG, encoding="utf-8"
            )
            rejecting = self._launcher(
                root,
                "old-mozyo-bridge",
                self._advertising(
                    "  grep -q by_lane_kind \"$4\" && "
                    "{ echo \"unknown key 'by_lane_kind'\" >&2; exit 2; }\n"
                ),
            )
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    rejecting,
                    subprocess.run,
                    _TIMEOUT,
                    dict(os.environ),
                    repo_root=root / "repo",
                    store_home=root / "home",
                )
            self.assertEqual(
                caught.exception.reason, LAUNCHER_CANNOT_PARSE_TARGET_CONFIG
            )
            self.assertIn("by_lane_kind", str(caught.exception))

    def test_a_launcher_that_accepts_it_is_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                self._NESTED_CONFIG, encoding="utf-8"
            )
            accepting = self._launcher(root, "new-mozyo-bridge", self._advertising())
            preflight_launcher_compatibility(
                accepting,
                subprocess.run,
                _TIMEOUT,
                dict(os.environ),
                repo_root=root / "repo",
                store_home=root / "home",
            )

    def test_a_config_broken_for_this_runtime_is_not_blamed_on_the_launcher(self) -> None:
        # The distinction review j#87752 required be preserved.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            TARGET_CONFIG_INVALID,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 2\nbogus_top_level: 1\n", encoding="utf-8"
            )
            accepting = self._launcher(root, "new-mozyo-bridge", self._advertising())
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    accepting,
                    subprocess.run,
                    _TIMEOUT,
                    dict(os.environ),
                    repo_root=root / "repo",
                    store_home=root / "home",
                )
            self.assertEqual(caught.exception.reason, TARGET_CONFIG_INVALID)
            self.assertIn("Fix the config", str(caught.exception))

    def test_the_real_cli_answers_the_probe_contract(self) -> None:
        # The two halves must agree: the argv the preflight builds is the command the CLI
        # registers, and its rejection exit code is the one the preflight expects.
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (  # noqa: E501
            CONFIG_CHECK_PARSE_REJECTED,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            CONFIG_PARSE_REJECTED_EXIT,
            build_config_parse_probe_argv,
        )

        self.assertEqual(CONFIG_PARSE_REJECTED_EXIT, CONFIG_CHECK_PARSE_REJECTED)
        self.assertEqual(
            build_config_parse_probe_argv("/x/launcher", "/tmp/c.yaml"),
            ["/x/launcher", "config", "check-parse", "--file", "/tmp/c.yaml"],
        )


# ---------------------------------------------------------------------------
# Review j#87762 / j#87766 blocking findings — R5 / R6 / R7.
# ---------------------------------------------------------------------------


def _current_head_launcher(directory: Path, name: str = "cur-mozyo-bridge") -> str:
    """A REAL launcher: a shell entrypoint that runs THIS source tree's CLI.

    Not a canned script. R6 is about what happens when a launcher's own startup parses the
    target config, which only a real CLI does — a fake that answers ``--help`` from a string
    cannot exhibit it, which is exactly why the previous round's tests missed the defect.
    """
    path = directory / name
    path.write_text(
        "#!/bin/sh\nexec " + sys.executable + " -c '"
        'import sys; sys.path.insert(0,"' + str(_SRC) + '"); '
        'sys.argv=["mozyo-bridge"]+sys.argv[1:]; '
        "from mozyo_bridge.application.cli import main; sys.exit(main())' \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class R5AmbiguousRefTest(unittest.TestCase):
    """R5: ambiguity must be proven, not inferred from git's (optional, localizable) warning."""

    def _repo_with_collision(self, root: Path, *, warn: bool) -> Path:
        repo = _seed_repo(root, _V2_CONFIG)
        _git(repo, "branch", "collision")
        _git(repo, "tag", "collision")
        # The ambient setting the previous implementation depended on. With it off git
        # resolves silently, so a warning-text check sees nothing to object to.
        _git(repo, "config", "core.warnAmbiguousRefs", "true" if warn else "false")
        return repo

    def test_an_ambiguous_name_yields_no_pin_regardless_of_ambient_config(self) -> None:
        for warn in (True, False):
            with self.subTest(warnAmbiguousRefs=warn):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = self._repo_with_collision(Path(tmp), warn=warn)
                    ops = LiveSublaneGitOperations(repo_root=repo)
                    self.assertEqual(
                        ops.resolve_commit("collision"),
                        "",
                        "an ambiguous base must never resolve to a pin",
                    )

    def test_the_ambiguity_refusal_mutates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_collision(Path(tmp), warn=False)
            before = _git(repo, "rev-parse", "HEAD").stdout
            refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout
            LiveSublaneGitOperations(repo_root=repo).resolve_commit("collision")
            self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout, before)
            self.assertEqual(
                _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
                refs_before,
            )
            self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")

    def test_unambiguous_shapes_still_pin(self) -> None:
        # The refusal must not be so broad that ordinary bases stop resolving.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_collision(Path(tmp), warn=False)
            ops = LiveSublaneGitOperations(repo_root=repo)
            for ref in ("main", "refs/heads/main", "HEAD"):
                with self.subTest(ref=ref):
                    self.assertRegex(ops.resolve_commit(ref), r"^[0-9a-f]{40}$")


class R8R9PseudoRefAmbiguityTest(unittest.TestCase):
    """R8 / R9: every name git calls ambiguous must be refused — including pseudo-refs.

    Two rounds of modelling git's pseudo-ref rule were wrong: R8 missed the
    ``$GIT_DIR/<name>`` candidate entirely, and R9 showed the follow-up rule (upper-case
    names) was the wrong criterion — ``.git/config`` is not a candidate because its *content*
    is not a ref, not because of its casing. These pin the outcome for all three shapes and,
    just as importantly, that ordinary bases are still pinned.
    """

    def _repo(self, root: Path) -> tuple[Path, str]:
        repo = _seed_repo(root, _V2_CONFIG)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        # R8: the pseudo-ref slot, which sits BEFORE every `refs/…` path in git's order.
        (repo / ".git" / "FETCH_HEAD").write_text(sha + "\n", encoding="utf-8")
        _git(repo, "branch", "FETCH_HEAD")
        # R9: a LOWER-case pseudo-ref whose content is a valid ref. The casing rule admitted
        # this one, which is why the rule is now git's own verdict rather than a model of it.
        (repo / ".git" / "whatever").write_text(sha + "\n", encoding="utf-8")
        _git(repo, "branch", "whatever")
        # R5: the plain branch/tag collision.
        _git(repo, "branch", "collision")
        _git(repo, "tag", "collision")
        # A lower-case `.git` file that is NOT a ref (INI), with a same-named branch. Git does
        # not call this ambiguous, so neither may we — over-refusing a legitimate base is its
        # own bug, and it is what a casing-shaped rule would have to get wrong in the other
        # direction to catch R9.
        _git(repo, "branch", "config")
        # The ambient setting every one of these bypasses depended on.
        _git(repo, "config", "core.warnAmbiguousRefs", "false")
        return repo, sha

    def test_every_ambiguous_shape_yields_no_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo(Path(tmp))
            ops = LiveSublaneGitOperations(repo_root=repo)
            for ref in ("FETCH_HEAD", "whatever", "collision"):
                with self.subTest(ref=ref):
                    self.assertEqual(
                        ops.resolve_commit(ref),
                        "",
                        f"git calls {ref!r} ambiguous; a pin must never be taken from it",
                    )

    def test_those_refusals_mutate_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo(Path(tmp))
            refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout
            head_before = _git(repo, "rev-parse", "HEAD").stdout
            ops = LiveSublaneGitOperations(repo_root=repo)
            for ref in ("FETCH_HEAD", "whatever", "collision"):
                ops.resolve_commit(ref)
            self.assertEqual(
                _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
                refs_before,
            )
            self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout, head_before)
            self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")

    def test_unambiguous_bases_are_still_pinned(self) -> None:
        # False-positive non-regression, pinned against git's OWN boundary. `config` is the
        # discriminating case: a `.git` file with a same-named branch that git does not treat
        # as a candidate, because its content is not a ref.
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self._repo(Path(tmp))
            ops = LiveSublaneGitOperations(repo_root=repo)
            for ref in ("config", "main", "HEAD", "refs/heads/main", sha):
                with self.subTest(ref=ref):
                    self.assertRegex(
                        ops.resolve_commit(ref),
                        r"^[0-9a-f]{40}$",
                        f"{ref!r} is unambiguous to git and must still pin",
                    )


class R6ConfigClassificationOrderTest(unittest.TestCase):
    """R6: the config classification must be reachable at the ``prepare_session`` callsite."""

    def _target(self, root: Path, config_text: str) -> Path:
        target = root / "target"
        (target / ".mozyo-bridge").mkdir(parents=True)
        (target / ".mozyo-bridge" / "config.yaml").write_text(config_text, encoding="utf-8")
        LaneLifecycleStore(home=root / "home").ensure_schema()
        return target

    def test_a_self_invalid_target_is_classified_not_blamed_on_the_launcher(self) -> None:
        # THE R6 regression. The capability probe is cwd-sensitive (#14231) and a CLI parses
        # the config at startup, so probing the target cwd FIRST turned this into "cannot run
        # the agent-attest subcommand" — blaming the launcher for the operator's config.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._target(root, "version: 2\nbogus_top_level: 1\n")
            launcher = _current_head_launcher(root)
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher, subprocess.run, 60.0, dict(os.environ),
                    repo_root=target, store_home=root / "home",
                )
            self.assertEqual(caught.exception.reason, TARGET_CONFIG_INVALID)

    def test_a_current_valid_target_a_real_old_parser_rejects_is_attributed_to_it(self) -> None:
        # The other half: valid HERE, rejected by the candidate. Must reach
        # `launcher_cannot_parse_target_config`, not a generic wrapper failure.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._target(root, R4NestedGrammarTest._NESTED_CONFIG)
            real = _current_head_launcher(root)
            # A launcher whose ADVERTISEMENT is this build's, but whose parser predates the
            # nested key — delegating everything else to the real CLI so the wrapper probe,
            # in every cwd, behaves exactly like a current launcher.
            old = root / "old-mozyo-bridge"
            old.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "config" ] && [ "$2" = "check-parse" ]; then\n'
                '  grep -q by_lane_kind "$4" && '
                "{ echo \"unknown key 'by_lane_kind'\" >&2; exit 2; }\n"
                "fi\n"
                f'exec "{real}" "$@"\n',
                encoding="utf-8",
            )
            old.chmod(old.stat().st_mode | stat.S_IEXEC)
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    str(old), subprocess.run, 60.0, dict(os.environ),
                    repo_root=target, store_home=root / "home",
                )
            self.assertEqual(
                caught.exception.reason, LAUNCHER_CANNOT_PARSE_TARGET_CONFIG
            )
            self.assertIn("by_lane_kind", str(caught.exception))

    def test_the_lane_cwd_probe_survives_the_reorder(self) -> None:
        # GUARD BITE for #14231: moving the advertisement read to a neutral cwd must not drop
        # the lane-cwd boundary. A launcher that is fine in a neutral cwd and broken in the
        # lane cwd for a NON-config reason must still be refused.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._target(root, _V2_CONFIG)
            real = _current_head_launcher(root)
            sentinel = target / "cwd-poison"
            sentinel.write_text("x", encoding="utf-8")
            cwd_broken = root / "cwd-broken-mozyo-bridge"
            cwd_broken.write_text(
                # cwd-sensitive by construction: the sentinel is only visible on a RELATIVE
                # path when the process actually runs in the lane directory. (`$PWD` would
                # not work — a subprocess `cwd=` changes the working directory but leaves
                # that inherited shell variable pointing at the parent's.)
                "#!/bin/sh\n"
                'if [ -f ./cwd-poison ] && [ "$1" = "herdr" ]; then\n'
                '  echo "boom in the lane cwd" >&2; exit 2\nfi\n'
                f'exec "{real}" "$@"\n',
                encoding="utf-8",
            )
            cwd_broken.chmod(cwd_broken.stat().st_mode | stat.S_IEXEC)
            with self.assertRaises(HerdrSessionStartError):
                preflight_launcher_compatibility(
                    str(cwd_broken), subprocess.run, 60.0, dict(os.environ),
                    repo_root=target, store_home=root / "home",
                )


class R7ProbePathRedactionTest(unittest.TestCase):
    """R7: the private probe path must never reach a public verdict."""

    _LEAKY = ("/var/", "/private/", "/tmp/", "mozyo-config-parse-", "\\")

    def _assert_no_path(self, text: str) -> None:
        for token in self._LEAKY:
            self.assertNotIn(token, text, f"public detail leaked {token!r}: {text!r}")

    def test_a_self_rejection_detail_carries_no_filesystem_path(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            CONFIG_TEXT_PRESENT,
            measure_config_parse_compatibility,
        )

        def _ok(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        observation = measure_config_parse_compatibility(
            "/bin/true", _ok, 5.0, {}, CONFIG_TEXT_PRESENT, "version: [1, 2\n"
        )
        self._assert_no_path(observation.launcher_detail)
        # The useful part survives: the reader still learns WHY it failed.
        self.assertIn("YAML", observation.launcher_detail)

    def test_a_candidate_rejection_detail_carries_no_filesystem_path(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            CONFIG_TEXT_PRESENT,
            measure_config_parse_compatibility,
        )

        def _reject(argv, **kwargs):
            # A candidate that echoes the probe path back, as a real CLI's error does.
            path = argv[argv.index("--file") + 1]
            return subprocess.CompletedProcess(
                argv, 2, stdout="", stderr=f"unknown key 'by_lane_kind' in {path}"
            )

        observation = measure_config_parse_compatibility(
            "/bin/true", _reject, 5.0, {}, CONFIG_TEXT_PRESENT, _V2_CONFIG
        )
        self._assert_no_path(observation.launcher_detail)
        self.assertIn("by_lane_kind", observation.launcher_detail)

    def test_the_public_exception_from_a_real_run_carries_no_path(self) -> None:
        # End to end through the real conjunction and a real launcher: the raised, public
        # error is what an operator sees, and it is what the close condition constrains.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            (target / ".mozyo-bridge").mkdir(parents=True)
            (target / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 2\nbogus_top_level: 1\n", encoding="utf-8"
            )
            LaneLifecycleStore(home=root / "home").ensure_schema()
            launcher = _current_head_launcher(root)
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher, subprocess.run, 60.0, dict(os.environ),
                    repo_root=target, store_home=root / "home",
                )
            message = str(caught.exception)
            self.assertNotIn(str(Path.home()), message)
            for token in ("mozyo-config-parse-", "/var/folders", "/private/var"):
                self.assertNotIn(token, message)


# ---------------------------------------------------------------------------
# Review j#87786 blocking findings — R10 / R11 / R12.
# ---------------------------------------------------------------------------


class R10RepoSelectionIsolationTest(unittest.TestCase):
    """R10: a probe's repo must be decided by its cwd alone, not by the ambient env."""

    def _target(self, root: Path, config_text: str) -> Path:
        target = root / "target"
        (target / ".mozyo-bridge").mkdir(parents=True)
        (target / ".mozyo-bridge" / "config.yaml").write_text(config_text, encoding="utf-8")
        LaneLifecycleStore(home=root / "home").ensure_schema()
        return target

    def test_the_selection_env_axis_is_the_one_the_resolver_documents(self) -> None:
        # DRIFT GUARD. The isolation below strips a specific set of variables; if the repo
        # resolver ever consults another one, that new axis would silently re-open exactly
        # the bypass R10 reported. Pin the set against the resolver's own source rather than
        # against a remembered list.
        import inspect

        from mozyo_bridge.shared import paths

        source = inspect.getsource(paths.resolve_repo_root)
        consulted = set(re.findall(r"os\.environ(?:\.get)?\(\s*[\"']([^\"']+)", source))
        self.assertEqual(
            consulted,
            set(REPO_SELECTION_ENV_VARS),
            "resolve_repo_root consults a different env axis than the probes isolate; "
            "add it to REPO_SELECTION_ENV_VARS or the ambient environment can re-bind a probe",
        )

    def test_repo_neutral_env_strips_only_the_selection_axis(self) -> None:
        env = {"MOZYO_REPO": "/somewhere", "PATH": "/usr/bin", "HOME": "/home/x"}
        self.assertEqual(repo_neutral_env(env), {"PATH": "/usr/bin", "HOME": "/home/x"})

    def test_a_self_invalid_target_is_classified_despite_an_ambient_mozyo_repo(self) -> None:
        # THE R10 regression: with `MOZYO_REPO` pointing at the target, the "neutral" cwd was
        # not neutral — the advertisement probe resolved the target's config and died there,
        # so the run reported a generic wrapper failure instead of the config classification.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._target(root, "version: 2\nbogus_top_level: 1\n")
            launcher = _current_head_launcher(root)
            env = dict(os.environ)
            env["MOZYO_REPO"] = str(target)
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher, subprocess.run, 60.0, env,
                    repo_root=target, store_home=root / "home",
                )
            self.assertEqual(caught.exception.reason, TARGET_CONFIG_INVALID)

    def test_a_broken_ambient_repo_does_not_misclassify_a_compatible_target(self) -> None:
        # The other direction: the measurement is about the target's bytes, so an unrelated
        # broken repo in `MOZYO_REPO` must not make a good launcher / good target look bad.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._target(root, _V2_CONFIG)
            broken = root / "broken"
            (broken / ".mozyo-bridge").mkdir(parents=True)
            (broken / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 2\nbogus_top_level: 1\n", encoding="utf-8"
            )
            launcher = _current_head_launcher(root)
            env = dict(os.environ)
            env["MOZYO_REPO"] = str(broken)
            preflight_launcher_compatibility(
                launcher, subprocess.run, 60.0, env,
                repo_root=target, store_home=root / "home",
            )


class R11WindowsPathRedactionTest(unittest.TestCase):
    """R11: the redaction backstop must cover the path shapes its contract claims."""

    def test_every_absolute_root_shape_is_redacted(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            REDACTED_PROBE_PATH,
            _redact_probe_paths,
        )

        # The home-shaped examples are COMPOSED, never written: `release check tree` fails a
        # tracked home-path literal even inside a redaction fixture — which is precisely the
        # leak this test is about, so writing one here would be the same defect one level up.
        backslash = chr(92)
        drive = "C:" + backslash + backslash.join(("Users", "alice", "c.yaml"))
        drive_fwd = "C:" + macos_home_path("alice", "c.yaml")
        unc = backslash * 2 + backslash.join(("server", "share", "c.yaml"))
        cases = (
            ("drive, backslash", f"error in {drive}: bad"),
            ("drive, forward", f"error in {drive_fwd}: bad"),
            ("UNC", f"error in {unc}: bad"),
            ("POSIX", f"error in {macos_home_path('alice', 'probe', 'config.yaml')}: bad"),
        )
        for label, text in cases:
            with self.subTest(shape=label):
                out = _redact_probe_paths(text, Path("/nonexistent"))
                self.assertIn(REDACTED_PROBE_PATH, out)
                self.assertNotIn("alice", out)

    def test_a_parse_reason_without_a_path_is_left_intact(self) -> None:
        # The backstop must not be so broad that it eats the information the detail exists
        # to carry — that is what makes the refusal actionable.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            REDACTED_PROBE_PATH,
            _redact_probe_paths,
        )

        for text in (
            "unknown key 'by_lane_kind'; allowed keys: ['default', 'sublane', 'version']",
            'while parsing a flow sequence in "<unicode string>", line 1, column 10',
            "lane_placement.by_lane_kind must be a mapping",
        ):
            with self.subTest(text=text[:40]):
                out = _redact_probe_paths(text, Path("/nonexistent"))
                self.assertEqual(out, text)
                self.assertNotIn(REDACTED_PROBE_PATH, out)


class R12ContractTextConsistencyTest(unittest.TestCase):
    """R12: the config axis is a direct measurement, and the prose must not say otherwise."""

    def test_no_surviving_declaration_join_claim_for_the_config_axis(self) -> None:
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_actuator_herdr_preflight,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_pane_lifecycle,
        )

        for module in (herdr_pane_lifecycle, sublane_actuator_herdr_preflight):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn("Both are *declaration* joins", source)
                self.assertNotIn("DECLARED join", source)

    def test_the_spec_describes_the_config_axis_as_a_measurement(self) -> None:
        spec = (_SRC.parent / "vibes" / "docs" / "specs" / "herdr-native-identity.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("config axis は宣言 join ではなく直接測定である", spec)
        self.assertNotIn(
            "config 側の probe は宣言 version と top-level key のみを読み",
            spec,
            "the retired summary-join description must not survive the R4 redesign",
        )


def _agent_attest_help(parser) -> str:
    """The rendered ``herdr agent-attest --help`` text, from the real composed parser."""
    stack = [parser]
    while stack:
        current = stack.pop()
        for action in current._actions:
            choices = getattr(action, "choices", None)
            if not isinstance(choices, dict):
                continue
            for name, sub in choices.items():
                if name == "agent-attest":
                    return sub.format_help()
                stack.append(sub)
    raise AssertionError("the `herdr agent-attest` subparser was not found")


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    unittest.main()
