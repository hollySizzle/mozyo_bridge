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

1. the two axes are verified by different means, both answerable *before* the lane worktree
   exists (unlike #14231's incidental "the launcher happens to exit non-zero in that cwd"):
   the lane lifecycle is a **declaration join** (``mozyo_attest_capability_lifecycle``), while
   the config is a **direct measurement** — the launcher's own parser is run over the exact
   target bytes, because every summary of the grammar was measured insufficient (j#87752 R4);
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

import dataclasses
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
            # Redmine #14203: the launch-generation protocol joined the conjunction for the
            # same reason as the rest — at the reserve boundary alone it let a skewed
            # launcher through this pre-worktree gate and left a worktree behind.
            "preflight_generation_protocol_capability",
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


class R13SymlinkMaterializationTest(unittest.TestCase):
    """R13: the measured bytes must be the bytes a checkout materializes."""

    def _hazard_repo(self, root: Path) -> tuple[Path, str]:
        """A base whose config is a SYMLINK, with the link payload a valid config document.

        The shape that reaches an admit: `git show` returns the link *target string*, which
        parses as a fine v2 config, while the checkout writes the linked file's contents — a
        different, unverified document.
        """
        repo = root / "primary"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        (repo / ".mozyo-bridge" / "version: 2").write_text(
            "version: 2\nbogus_top_level: 1\n", encoding="utf-8"
        )
        (repo / ".mozyo-bridge" / "config.yaml").symlink_to("version: 2")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c1")
        return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_a_non_regular_entry_is_reported_as_such_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self._hazard_repo(Path(tmp))
            state, text = LiveSublaneGitOperations(repo_root=repo).committed_blob(
                ref=sha, relpath=".mozyo-bridge/config.yaml"
            )
            self.assertEqual(state, "blob_not_regular")
            self.assertEqual(text, "")

    def test_the_hazard_reaches_a_typed_refusal_with_zero_actuation(self) -> None:
        """The conjunction the close condition needs, driven through the real use case.

        An earlier version of this test called the read-only helper and then compared refs
        before/after — which a read-only call cannot change, so it proved nothing about the
        gate (design consultation j#87802 point 1). This drives ``SublaneActuateUseCase``
        against a spy port that RAISES on every mutation, so "no worktree, no branch, no pane"
        is established by the actuation boundary itself rather than by a snapshot.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self._hazard_repo(root)
            git_ops = LiveSublaneGitOperations(repo_root=repo)
            calls: list = []

            class _SpyOps:
                """Every mutating port method raises; the reads are the real git ones."""

                def is_git_workspace(self) -> bool:
                    return True

                def worktree_exists(self, branch: str) -> bool:
                    return False

                def resolve_base_commit(self, ref: str) -> str:
                    return git_ops.resolve_commit(ref)

                def preflight_launcher_compatibility(self, **kwargs):
                    calls.append("preflight")
                    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
                        read_lane_target_config_text,
                    )

                    state, _ = read_lane_target_config_text(
                        git_ops.committed_blob,
                        base_commit=kwargs["base_commit"],
                        lane_runtime_root=kwargs["lane_runtime_root"],
                        from_base_ref=kwargs["from_base_ref"],
                    )
                    if state != "config_text_present":
                        return False, "target_config_invalid", "hazard base is not measurable"
                    return True, "", "ok"

                def create_worktree(self, **kwargs):
                    raise AssertionError("git worktree add reached on a hazard base")

                def append_lane_column(self, worktree_path: str):
                    raise AssertionError("pane creation reached on a hazard base")

                def append_lane_argv(self, worktree_path: str) -> list:
                    return []

                def read_lane(self, worktree_path: str):
                    raise AssertionError("lane read-back reached on a hazard base")

                def declare_adopted_lane_lifecycle(self, worktree_path: str, *, adopted: bool):
                    raise AssertionError("lifecycle write reached on a hazard base")

                def probe_gateway_ready(self, gateway_pane: str) -> bool:
                    return False

                def dispatch_implementation_request(self, **kwargs) -> int:
                    raise AssertionError("dispatch reached on a hazard base")

            refs_before = _git(repo, "for-each-ref", "--format=%(refname)").stdout
            worktrees_before = _git(repo, "worktree", "list", "--porcelain").stdout

            outcome = SublaneActuateUseCase(_SpyOps(), gateway_ready_probes=0).run(
                SublaneCreateRequest(
                    issue="14258",
                    lane_label="issue_14258_hazard",
                    branch="issue_14258_hazard",
                    worktree_path=str(root / "lane-that-must-not-exist"),
                    journal="87796",
                    base_ref=sha,
                ),
                execute=True,
                dispatch=False,
                target_repo=str(root / "lane-that-must-not-exist"),
            )

            self.assertTrue(outcome.is_blocked)
            self.assertIn("launcher_runtime_incompatible", outcome.blocked_reasons)
            self.assertIn(TARGET_CONFIG_INVALID, outcome.blocked_reasons)
            self.assertEqual(calls, ["preflight"])
            # Nothing was created: no worktree directory, and git's own view is unchanged.
            self.assertFalse((root / "lane-that-must-not-exist").exists())
            self.assertEqual(
                _git(repo, "for-each-ref", "--format=%(refname)").stdout, refs_before
            )
            self.assertEqual(
                _git(repo, "worktree", "list", "--porcelain").stdout, worktrees_before
            )

    def test_the_measured_bytes_would_have_differed_from_the_checkout(self) -> None:
        # Proves the premise the fix rests on, so the pin is not merely asserting the new
        # return value: what `git show` yields is NOT what `git worktree add` writes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self._hazard_repo(root)
            shown = _git(repo, "show", f"{sha}:.mozyo-bridge/config.yaml").stdout
            lane = root / "lane"
            _git(repo, "worktree", "add", "-q", str(lane), "-b", "lane_x", sha)
            materialized = (lane / ".mozyo-bridge" / "config.yaml").read_text()
            self.assertNotEqual(shown.strip(), materialized.strip())
            self.assertIn("bogus_top_level", materialized)

    def test_a_regular_blob_is_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _seed_repo(Path(tmp), _V2_CONFIG)
            sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
            state, text = LiveSublaneGitOperations(repo_root=repo).committed_blob(
                ref=sha, relpath=".mozyo-bridge/config.yaml"
            )
            self.assertEqual(state, "blob_present")
            self.assertIn("agents", text)


class R13CheckoutTransformTest(unittest.TestCase):
    """R13 (consultation j#87804): a REGULAR blob can still be transformed at checkout."""

    def _filtered_repo(self, root: Path, attributes: str) -> tuple[Path, str]:
        repo = root / "primary"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        (repo / ".mozyo-bridge" / "config.yaml").write_text(_V2_CONFIG, encoding="utf-8")
        (repo / ".gitattributes").write_text(attributes + "\n", encoding="utf-8")
        # A smudge filter that makes the checkout differ from the blob, so the two can be
        # told apart by measurement rather than by argument.
        _git(repo, "config", "filter.inject.smudge", 'sed "s/version: 2/version: [/"')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c1")
        return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_the_blob_is_not_the_materialized_bytes_under_a_smudge_filter(self) -> None:
        # The premise, measured: mode is an ordinary regular blob, yet the checkout differs.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self._filtered_repo(root, ".mozyo-bridge/config.yaml filter=inject")
            entry = _git(repo, "ls-tree", sha, "--", ".mozyo-bridge/config.yaml").stdout
            self.assertTrue(entry.startswith("100644 blob"), entry)
            raw = _git(repo, "show", f"{sha}:.mozyo-bridge/config.yaml").stdout
            lane = root / "lane"
            _git(repo, "worktree", "add", "-q", str(lane), "-b", "lane_x", sha)
            materialized = (lane / ".mozyo-bridge" / "config.yaml").read_text()
            self.assertNotEqual(raw, materialized)

    def test_a_transformable_path_is_refused_before_any_mutation(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            read_lane_target_config_text,
        )

        for attributes in (
            ".mozyo-bridge/config.yaml filter=inject",
            "* text=auto",  # the common shape; conservative refusal is deliberate
        ):
            with self.subTest(attributes=attributes):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo, sha = self._filtered_repo(root, attributes)
                    ops = LiveSublaneGitOperations(repo_root=repo)
                    self.assertEqual(
                        ops.committed_blob(
                            ref=sha, relpath=".mozyo-bridge/config.yaml"
                        )[0],
                        "blob_may_be_transformed",
                    )
                    state, text = read_lane_target_config_text(
                        ops.committed_blob,
                        base_commit=sha,
                        lane_runtime_root="",
                        from_base_ref=True,
                    )
                    # Distinct from "unreadable" on purpose (consultation j#87807): the
                    # config may be valid, so the refusal must not claim it is broken.
                    self.assertEqual(state, "config_text_transform_unverifiable")
                    self.assertIsNone(text)

    def test_the_attribute_source_is_the_pinned_commit_not_the_working_tree(self) -> None:
        """The distinction that ruled out ``cat-file --filters`` (consultation j#87804).

        With the attributes committed but deleted from the working tree, ``--filters`` returns
        the RAW blob while ``git worktree add`` still transforms — so the bytes it reports are
        not the lane's. This pins that the refusal survives that exact configuration.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self._filtered_repo(root, ".mozyo-bridge/config.yaml filter=inject")
            (repo / ".gitattributes").unlink()

            filtered = _git(
                repo, "cat-file", "--filters",
                "--path=.mozyo-bridge/config.yaml", f"{sha}:.mozyo-bridge/config.yaml",
            ).stdout
            lane = root / "lane"
            _git(repo, "worktree", "add", "-q", str(lane), "-b", "lane_x", sha)
            materialized = (lane / ".mozyo-bridge" / "config.yaml").read_text()
            self.assertNotEqual(
                filtered, materialized, "this is why --filters is not the authority"
            )
            self.assertEqual(
                LiveSublaneGitOperations(repo_root=repo).committed_blob(
                    ref=sha, relpath=".mozyo-bridge/config.yaml"
                )[0],
                "blob_may_be_transformed",
            )

    def test_an_untransformed_repo_is_still_read(self) -> None:
        # The admission side: with no attributes the blob IS the materialized bytes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _seed_repo(root, _V2_CONFIG)
            sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
            state, text = LiveSublaneGitOperations(repo_root=repo).committed_blob(
                ref=sha, relpath=".mozyo-bridge/config.yaml"
            )
            self.assertEqual(state, "blob_present")
            lane = root / "lane"
            _git(repo, "worktree", "add", "-q", str(lane), "-b", "lane_x", sha)
            self.assertEqual(
                text, (lane / ".mozyo-bridge" / "config.yaml").read_text()
            )


class R13PublicReasonTest(unittest.TestCase):
    """j#87807 / j#87809: an unverifiable materialization must not read as a broken config.

    Both causes — a checkout conversion (j#87807) and a non-regular entry (j#87809) — are the
    same class: the bytes the lane will receive cannot be established. Neither may be reported
    as "your config does not parse", and each must name its own real recovery.

    The reason is produced by the REAL evaluator here, not supplied by a spy (j#87809), so the
    coverage is of the code that actually runs.
    """

    def _repo(self, root: Path, *, cause: str) -> tuple[Path, str]:
        repo = root / "primary"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        if cause == "transform":
            # A PERFECTLY VALID committed config: validity is not what is in question.
            (repo / ".mozyo-bridge" / "config.yaml").write_text(_V2_CONFIG, encoding="utf-8")
            (repo / ".gitattributes").write_text(
                ".mozyo-bridge/config.yaml filter=inject\n", encoding="utf-8"
            )
            _git(repo, "config", "filter.inject.smudge", 'sed "s/version: 2/version: [/"')
        else:
            (repo / ".mozyo-bridge" / "real.yaml").write_text(_V2_CONFIG, encoding="utf-8")
            (repo / ".mozyo-bridge" / "config.yaml").symlink_to("real.yaml")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c1")
        return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()

    def _evaluate(self, root: Path, repo: Path, sha: str):
        """Drive the REAL evaluator — the function the herdr ops adapter delegates to."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            evaluate_launcher_compatibility,
        )

        LaneLifecycleStore(home=root / "home").ensure_schema()
        return evaluate_launcher_compatibility(
            env=dict(os.environ),
            runner=subprocess.run,
            timeout=60.0,
            repo_root=repo,
            store_home=root / "home",
            replacement_action_id="",
            committed_blob=LiveSublaneGitOperations(repo_root=repo).committed_blob,
            base_commit=sha,
            lane_runtime_root="",
            from_base_ref=True,
        )

    def test_both_causes_produce_the_typed_unverifiable_refusal(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            TARGET_CONFIG_UNVERIFIABLE,
        )

        expectations = {
            "transform": "checkout conversion",
            "not_regular": "not a regular file",
        }
        for cause, phrase in expectations.items():
            with self.subTest(cause=cause):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo, sha = self._repo(root, cause=cause)
                    ok, reason, detail = self._evaluate(root, repo, sha)

                    self.assertFalse(ok)
                    self.assertEqual(reason, TARGET_CONFIG_UNVERIFIABLE)
                    # It says something TRUE about this repo, and names THIS cause.
                    self.assertIn(phrase, detail)
                    self.assertIn("Recovery:", detail)
                    # The two false claims the collapsed reason used to make.
                    self.assertNotIn("does not parse under THIS runtime", detail)
                    self.assertNotIn("changing the launcher will not help", detail)
                    # No private internals.
                    for forbidden in (
                        "filter=inject", "check-attr", "gitattributes", "real.yaml", str(repo)
                    ):
                        self.assertNotIn(forbidden, detail)

    def test_the_refusal_reaches_the_actuator_outcome_with_zero_mutation(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            TARGET_CONFIG_UNVERIFIABLE,
        )

        for cause in ("transform", "not_regular"):
            with self.subTest(cause=cause):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo, sha = self._repo(root, cause=cause)
                    case = self
                    git_ops = LiveSublaneGitOperations(repo_root=repo)

                    class _Ops:
                        """Reads are real; the REASON comes from the real evaluator; every
                        mutation raises."""

                        def is_git_workspace(self) -> bool:
                            return True

                        def worktree_exists(self, branch: str) -> bool:
                            return False

                        def resolve_base_commit(self, ref: str) -> str:
                            return git_ops.resolve_commit(ref)

                        def preflight_launcher_compatibility(self, **kwargs):
                            return case._evaluate(root, repo, kwargs["base_commit"])

                        def create_worktree(self, **kwargs):
                            raise AssertionError("worktree created on an unverifiable base")

                        def append_lane_column(self, worktree_path: str):
                            raise AssertionError("pane created on an unverifiable base")

                        def append_lane_argv(self, worktree_path: str) -> list:
                            return []

                        def read_lane(self, worktree_path: str):
                            raise AssertionError("read-back on an unverifiable base")

                        def declare_adopted_lane_lifecycle(self, worktree_path, *, adopted):
                            raise AssertionError("lifecycle write on an unverifiable base")

                        def probe_gateway_ready(self, gateway_pane: str) -> bool:
                            return False

                        def dispatch_implementation_request(self, **kwargs) -> int:
                            raise AssertionError("dispatch on an unverifiable base")

                    lane = root / "must-not-exist"
                    refs_before = _git(repo, "for-each-ref", "--format=%(refname)").stdout
                    outcome = SublaneActuateUseCase(_Ops(), gateway_ready_probes=0).run(
                        SublaneCreateRequest(
                            issue="14258", lane_label="issue_14258_x",
                            branch="issue_14258_x", worktree_path=str(lane),
                            journal="87809", base_ref=sha,
                        ),
                        execute=True, dispatch=False, target_repo=str(lane),
                    )
                    self.assertTrue(outcome.is_blocked)
                    self.assertIn(TARGET_CONFIG_UNVERIFIABLE, outcome.blocked_reasons)
                    self.assertFalse(lane.exists())
                    self.assertEqual(
                        _git(repo, "for-each-ref", "--format=%(refname)").stdout, refs_before
                    )

    def test_an_unanswerable_attribute_query_also_fails_closed(self) -> None:
        """The fail-closed branch a mutation probe showed nothing pinned.

        `check-attr` failing is not "no attributes" — the question simply went unanswered,
        and the whole point of this gate is that an unanswered question is a refusal.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _seed_repo(Path(tmp), _V2_CONFIG)
            sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

            class _QueryFails(LiveSublaneGitOperations):
                def _run(self, *args):
                    if args and args[0] == "check-attr":
                        return subprocess.CompletedProcess(list(args), 128, "", "boom")
                    return super()._run(*args)

            failing = _QueryFails(repo_root=repo)
            self.assertEqual(
                failing._checkout_transform_state(sha, ".mozyo-bridge/config.yaml"),
                "transform_unknown",
            )
            # Refused — but as its OWN fact: an unanswered query is not an observed
            # conversion (consultation j#87811).
            self.assertEqual(
                failing.committed_blob(ref=sha, relpath=".mozyo-bridge/config.yaml")[0],
                "blob_transform_unknown",
                "an unanswered attribute query must refuse, not read as 'no attributes'",
            )

    def test_query_unanswerable_and_observed_conversion_say_different_things(self) -> None:
        """Both refuse, but the public evidence must not claim an unobserved conversion."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            read_lane_target_config_text,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            CONFIG_PARSE_CONTRACT_VERSION,
            TARGET_CONFIG_UNVERIFIABLE,
            decide_config_parse_compatibility,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            measure_config_parse_compatibility,
        )

        def _detail_for(blob_reader) -> str:
            state, text = read_lane_target_config_text(
                blob_reader, base_commit=sha, lane_runtime_root="", from_base_ref=True
            )
            observation = measure_config_parse_compatibility(
                "/bin/true", lambda *a, **k: None, 5.0, {}, state, text
            )
            verdict = decide_config_parse_compatibility(
                _observation(_capable_help()),
                observation,
                required_contract_version=CONFIG_PARSE_CONTRACT_VERSION,
            )
            self.assertFalse(verdict.ok)
            self.assertEqual(verdict.reason, TARGET_CONFIG_UNVERIFIABLE)
            return verdict.detail

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "primary"
            (repo / ".mozyo-bridge").mkdir(parents=True)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@example.invalid")
            _git(repo, "config", "user.name", "t")
            (repo / ".mozyo-bridge" / "config.yaml").write_text(_V2_CONFIG, encoding="utf-8")
            (repo / ".gitattributes").write_text(
                ".mozyo-bridge/config.yaml filter=inject\n", encoding="utf-8"
            )
            _git(repo, "config", "filter.inject.smudge", 'sed "s/version: 2/version: [/"')
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "c1")
            sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

            class _QueryFails(LiveSublaneGitOperations):
                def _run(self, *args):
                    if args and args[0] == "check-attr":
                        return subprocess.CompletedProcess(list(args), 128, "", "boom")
                    return super()._run(*args)

            observed = _detail_for(LiveSublaneGitOperations(repo_root=repo).committed_blob)
            unknown = _detail_for(_QueryFails(repo_root=repo).committed_blob)

            self.assertIn("checkout conversion applies", observed)
            # The unobserved case must NOT claim one was seen.
            self.assertNotIn("checkout conversion applies", unknown)
            self.assertIn("could not establish", unknown)
            self.assertIn("Recovery:", unknown)
            self.assertNotEqual(observed, unknown)

    def test_the_blob_state_vocabulary_is_fully_exported(self) -> None:
        # j#87809: `BLOB_NOT_REGULAR` trailed the other states outside `__all__`.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_integration,
        )

        for name in dir(sublane_integration):
            if name.startswith("BLOB_"):
                with self.subTest(state=name):
                    self.assertIn(name, sublane_integration.__all__)


class R17AttributeSentinelTest(unittest.TestCase):
    """R17: an attribute VALUE that spells a sentinel must not read as a state."""

    def _repo(self, root: Path, attribute_value: str) -> tuple[Path, str]:
        repo = root / "primary"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        (repo / ".mozyo-bridge" / "config.yaml").write_text(_V2_CONFIG, encoding="utf-8")
        (repo / ".gitattributes").write_text(
            f".mozyo-bridge/config.yaml filter={attribute_value}\n", encoding="utf-8"
        )
        # A driver NAMED like the sentinel: legal, and it really converts.
        _git(repo, "config", f"filter.{attribute_value}.smudge",
             'sed "s/version: 2/version: [/"')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c1")
        return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_sentinel_named_drivers_are_refused(self) -> None:
        for value in ("unset", "unspecified"):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo, sha = self._repo(root, value)
                    ops = LiveSublaneGitOperations(repo_root=repo)
                    self.assertEqual(
                        ops.committed_blob(ref=sha, relpath=".mozyo-bridge/config.yaml")[0],
                        "blob_may_be_transformed",
                        f"a filter driver named {value!r} really converts the checkout",
                    )
                    # And it really would have: prove the hazard, not just the verdict.
                    lane = root / f"lane-{value}"
                    _git(repo, "worktree", "add", "-q", str(lane), "-b", f"l_{value}", sha)
                    self.assertIn(
                        "version: [", (lane / ".mozyo-bridge" / "config.yaml").read_text()
                    )

    def test_a_repo_with_no_attributes_is_still_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _seed_repo(Path(tmp), _V2_CONFIG)
            sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(
                LiveSublaneGitOperations(repo_root=repo).committed_blob(
                    ref=sha, relpath=".mozyo-bridge/config.yaml"
                )[0],
                "blob_present",
            )

    def test_the_sentinel_repo_reaches_a_typed_refusal_with_zero_mutation(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            evaluate_launcher_compatibility,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            TARGET_CONFIG_UNVERIFIABLE,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self._repo(root, "unset")
            LaneLifecycleStore(home=root / "home").ensure_schema()
            git_ops = LiveSublaneGitOperations(repo_root=repo)

            class _Ops:
                def is_git_workspace(self) -> bool:
                    return True

                def worktree_exists(self, branch: str) -> bool:
                    return False

                def resolve_base_commit(self, ref: str) -> str:
                    return git_ops.resolve_commit(ref)

                def preflight_launcher_compatibility(self, **kwargs):
                    return evaluate_launcher_compatibility(
                        env=dict(os.environ), runner=subprocess.run, timeout=60.0,
                        repo_root=repo, store_home=root / "home",
                        replacement_action_id="",
                        committed_blob=git_ops.committed_blob,
                        base_commit=kwargs["base_commit"],
                        lane_runtime_root=kwargs["lane_runtime_root"],
                        from_base_ref=kwargs["from_base_ref"],
                    )

                def create_worktree(self, **kwargs):
                    raise AssertionError("worktree created on a sentinel-attribute base")

                def append_lane_column(self, worktree_path: str):
                    raise AssertionError("pane created on a sentinel-attribute base")

                def append_lane_argv(self, worktree_path: str) -> list:
                    return []

                def read_lane(self, worktree_path: str):
                    raise AssertionError("read-back on a sentinel-attribute base")

                def declare_adopted_lane_lifecycle(self, worktree_path, *, adopted):
                    raise AssertionError("lifecycle write on a sentinel-attribute base")

                def probe_gateway_ready(self, gateway_pane: str) -> bool:
                    return False

                def dispatch_implementation_request(self, **kwargs) -> int:
                    raise AssertionError("dispatch on a sentinel-attribute base")

            lane = root / "must-not-exist"
            refs_before = _git(repo, "for-each-ref", "--format=%(refname)").stdout
            outcome = SublaneActuateUseCase(_Ops(), gateway_ready_probes=0).run(
                SublaneCreateRequest(
                    issue="14258", lane_label="issue_14258_x", branch="issue_14258_x",
                    worktree_path=str(lane), journal="87817", base_ref=sha,
                ),
                execute=True, dispatch=False, target_repo=str(lane),
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(TARGET_CONFIG_UNVERIFIABLE, outcome.blocked_reasons)
            self.assertFalse(lane.exists())
            self.assertEqual(
                _git(repo, "for-each-ref", "--format=%(refname)").stdout, refs_before
            )
            # No attribute value in the public evidence.
            for forbidden in ("unset", "filter", "gitattributes"):
                self.assertNotIn(forbidden, outcome.reason)


class R19UndecodableConfigTest(unittest.TestCase):
    """R19: non-UTF-8 committed bytes must reach a typed state, not raise."""

    def _repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "primary"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        (repo / ".mozyo-bridge" / "config.yaml").write_bytes(
            b"version: 2\n\xff\xfe not utf-8\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c1")
        return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_it_becomes_a_typed_state_rather_than_an_exception(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            read_lane_target_config_text,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self._repo(Path(tmp))
            ops = LiveSublaneGitOperations(repo_root=repo)
            self.assertEqual(
                ops.committed_blob(ref=sha, relpath=".mozyo-bridge/config.yaml")[0],
                "blob_undecodable",
            )
            state, text = read_lane_target_config_text(
                ops.committed_blob, base_commit=sha, lane_runtime_root="",
                from_base_ref=True,
            )
            self.assertEqual(state, "config_text_undecodable")
            self.assertIsNone(text)

    def test_both_call_sites_agree_on_undecodable_bytes(self) -> None:
        # The asymmetry R19 reported: the worktree path already failed closed here.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            read_target_config_text,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_bytes(b"version: 2\n\xff\xfe\n")
            self.assertEqual(read_target_config_text(root)[0], "config_text_unreadable")

    def test_the_public_reason_claims_nothing_about_validity(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
            CONFIG_PARSE_CONTRACT_VERSION,
            TARGET_CONFIG_UNVERIFIABLE,
            decide_config_parse_compatibility,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            CONFIG_TEXT_UNDECODABLE,
            measure_config_parse_compatibility,
        )

        observation = measure_config_parse_compatibility(
            "/bin/true", lambda *a, **k: None, 5.0, {}, CONFIG_TEXT_UNDECODABLE, None
        )
        verdict = decide_config_parse_compatibility(
            _observation(_capable_help()), observation,
            required_contract_version=CONFIG_PARSE_CONTRACT_VERSION,
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, TARGET_CONFIG_UNVERIFIABLE)
        self.assertIn("not decodable", verdict.detail)
        self.assertIn("Recovery:", verdict.detail)
        self.assertNotIn("does not parse under THIS runtime", verdict.detail)


class R21ExternalUrlCollapseTest(unittest.TestCase):
    """A ``scheme://`` token is collapsed to a fixed placeholder, not preserved (j#87837).

    A URL's path, query and fragment can carry a filesystem path, and nothing structural
    separates ``https://host/docs/schema`` from ``https://host/docs/Users/<name>/private.yaml``.
    Preserving the token therefore means either modelling its content or accepting a hole, and
    the close condition allows neither — so public evidence keeps the fact that a URL was
    printed and drops what it said.
    """

    def _redact(self, text: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (  # noqa: E501
            _redact_probe_paths,
        )

        return _redact_probe_paths(text, Path("/nonexistent"))

    def _placeholder(self) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (  # noqa: E501
            EXTERNAL_URL_PLACEHOLDER,
        )

        return EXTERNAL_URL_PLACEHOLDER

    def test_no_url_body_reaches_public_evidence(self) -> None:
        private = macos_home_path("Ada Smith", "private.yaml")
        for label, text in (
            ("path embeds a private path", "see https://example.invalid/docs" + private),
            ("query names it", "see https://example.invalid/d?file=" + private),
            ("fragment names it", "see https://example.invalid/d#" + private),
            ("file:// URL", "see file://" + private),
            ("host only", "see https://example.invalid/docs/schema for keys"),
            ("port", "see http://example.invalid:8080/docs for keys"),
            ("two on a line", "see https://a.invalid/x and http://b.invalid/y ok"),
        ):
            with self.subTest(shape=label):
                out = self._redact(text)
                self.assertIn(self._placeholder(), out)
                for leaked in ("Ada", "Smith", "private.yaml", "Users", "invalid", "docs"):
                    self.assertNotIn(leaked, out, f"{label}: {out!r}")

    def test_a_url_takes_the_rest_of_the_line_with_it(self) -> None:
        # A URL cannot contain a raw space, but a private path can — so a token that broke at a
        # space may have been a path all along, and its tail would read as a relative token.
        # Measured before this rule: `see <external URL> Smith/private.yaml`. A URL cannot
        # contain a raw space; a private path can, and the tail loses its root to the
        # placeholder, so the scanner behind it cannot recognize what is left.
        out = self._redact("see https://example.invalid/docs/Users/Ada Smith/private.yaml")
        self.assertEqual(out, "see " + self._placeholder())

    def test_a_bare_authority_url_also_takes_the_rest_of_the_line(self) -> None:
        # j#87841 removed the "only if it bears a path" carve-out. A bare authority followed by
        # a query is exactly where the carve-out failed: `?file=C:\\Users\\Ada Smith\\…` has no
        # forward slash, so the line was not truncated and the tail after the space survived.
        backslash = chr(92)
        for label, tail in (
            ("drive in query",
             "?file=C:" + backslash + backslash.join(("Users", "Ada Smith", "private.yaml"))),
            ("UNC in query",
             "?file=" + backslash * 2 + backslash.join(("srv", "Ada Share", "private.yaml"))),
            ("POSIX in query", "?file=" + macos_home_path("Ada Smith", "private.yaml")),
            ("prose after", " for the key list"),
        ):
            with self.subTest(shape=label):
                out = self._redact("see https://host" + tail)
                self.assertEqual(out, "see " + self._placeholder())

    def test_no_end_of_url_rule_is_relied_on_for_privacy(self) -> None:
        """The URL's extent must not decide anything (j#87841).

        Both rejected rules were end-of-token rules, and each leaked on a shape the other did
        not. Pinning the absence: whatever separator or content follows ``scheme://``, the line
        ends at the placeholder.
        """
        private = macos_home_path("Ada Smith", "private.yaml")
        for label, sep in (
            ("tab", chr(9)),
            ("non-breaking space", chr(160)),
            ("ideographic space", chr(12288)),
            ("narrow no-break space", chr(8239)),
            ("zero-width space", chr(8203)),
            ("literal space", " "),
            ("no separator", ""),
        ):
            with self.subTest(separator=label):
                out = self._redact("see https://example.invalid" + sep + private)
                self.assertEqual(out, "see " + self._placeholder())

    def test_the_token_ends_at_unicode_whitespace_not_at_a_literal_space(self) -> None:
        # The R21 defect in one line: a rule that only knows `" "`. Each of these separators
        # starts new text the URL rule must not absorb silently — and whatever it does absorb
        # must be replaced, never emitted.
        private = macos_home_path("Ada Smith", "private.yaml")
        for label, sep in (
            ("tab", chr(9)),
            ("non-breaking space", chr(160)),
            ("form feed", chr(12)),
            ("vertical tab", chr(11)),
            ("ideographic space", chr(12288)),
            ("next line", chr(133)),
            ("zero-width space", chr(8203)),
        ):
            with self.subTest(separator=label):
                out = self._redact("see https://example.invalid/d" + sep + private)
                for leaked in ("Ada", "Smith", "private.yaml", "Users"):
                    self.assertNotIn(leaked, out, f"{label}: {out!r}")


class R21ProofIsLocalToOneRootTest(unittest.TestCase):
    """R21: a positive proof covers ONE root occurrence, never the token around it.

    The R20 guard skipped to the end of the whitespace-delimited token once any proof held —
    and its idea of "whitespace" was a literal space. So a tab, a non-breaking space or a
    ``?file=`` inside the same token carried a full private path straight through a rule whose
    whole premise is that unproven roots are private (review j#87831, all measured).
    """

    def _redact(self, text: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (  # noqa: E501
            _redact_probe_paths,
        )

        return _redact_probe_paths(text, Path("/nonexistent"))

    _PRIVATE = ("Ada", "Smith", "Team", "Share", "srv", "private.yaml", "p.yaml", "Users")

    def _roots(self) -> tuple:
        backslash = chr(92)
        return (
            ("POSIX", macos_home_path("Ada Smith", "private.yaml")),
            ("drive", "C:" + backslash + backslash.join(("Users", "Ada Smith", "private.yaml"))),
            ("UNC", backslash * 2 + backslash.join(("srv", "Ada Share", "p.yaml"))),
        )

    def _separators(self) -> tuple:
        # Everything that put a second root inside one "token". The literal space is included
        # as the case the old rule DID handle, so the sweep covers the whole class.
        return (
            ("tab", chr(9)),
            ("non-breaking space", chr(160)),
            ("form feed", chr(12)),
            ("vertical tab", chr(11)),
            ("URL query", "?file="),
            ("colon label", " config:"),
            ("space", " "),
        )

    def test_no_separator_after_a_proven_token_exempts_a_later_root(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (  # noqa: E501
            EXTERNAL_URL_PLACEHOLDER,
            REDACTED_PROBE_PATH,
        )

        # Both proof kinds have to be swept: a URL proof and a relative-token proof.
        for proven in ("see https://example.invalid/docs", "reason relative/path.yaml"):
            for sep_label, sep in self._separators():
                for root_label, root in self._roots():
                    text = proven + sep + root
                    with self.subTest(proven=proven[:12], sep=sep_label, root=root_label):
                        out = self._redact(text)
                        self.assertTrue(
                            REDACTED_PROBE_PATH in out or EXTERNAL_URL_PLACEHOLDER in out,
                            f"nothing was redacted at all: {text!r} -> {out!r}",
                        )
                        for leaked in self._PRIVATE:
                            self.assertNotIn(leaked, out, f"{text!r} -> {out!r}")

    def test_the_prose_before_a_collapsed_url_survives(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (  # noqa: E501
            EXTERNAL_URL_PLACEHOLDER,
        )

        out = self._redact(
            "see https://example.invalid/docs" + chr(9) + macos_home_path("Ada Smith", "p.yaml")
        )
        self.assertEqual(out, "see " + EXTERNAL_URL_PLACEHOLDER)

    def test_a_file_url_naming_a_private_path_is_redacted(self) -> None:
        out = self._redact("see file://" + macos_home_path("Ada Smith", "private.yaml"))
        for leaked in self._PRIVATE:
            self.assertNotIn(leaked, out)

    def test_relative_preservation_is_unchanged(self) -> None:
        for text in (
            "unknown key in relative/path.yaml",
            "expected one of ['down/right'] for split",
            "lane_placement.by_lane_kind must be a mapping",
        ):
            with self.subTest(text=text[:38]):
                self.assertEqual(self._redact(text), text)

    def test_the_public_refusal_carries_no_private_tail_and_mutates_nothing(self) -> None:
        # Required fix 4: re-confirm through the REAL preflight, not the helper alone.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction import (  # noqa: E501
            REDACTED_PROBE_PATH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Ada Team scratch"
            repo = root / "repo"
            (repo / ".mozyo-bridge").mkdir(parents=True)
            (repo / ".mozyo-bridge" / "config.yaml").write_text("version: [1, 2\n", encoding="utf-8")
            LaneLifecycleStore(home=root / "home").ensure_schema()
            launcher = _current_head_launcher(root)
            # Snapshot AFTER the launcher script is written: the fixture's own setup is not the
            # mutation under test, the preflight's behaviour is.
            before = sorted(p.name for p in root.rglob("*"))
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher, subprocess.run, 60.0, dict(os.environ),
                    repo_root=repo, store_home=root / "home",
                )
            message = str(caught.exception)
            self.assertEqual(caught.exception.reason, TARGET_CONFIG_INVALID)
            self.assertIn(REDACTED_PROBE_PATH, message)
            for leaked in (str(root), "Ada Team scratch", "mozyo-config-parse-", str(Path.home())):
                self.assertNotIn(leaked, message)
            self.assertEqual(sorted(p.name for p in root.rglob("*")), before)


class R20UntrustedShapeRedactionTest(unittest.TestCase):
    """R20: an absolute root is private unless something POSITIVELY proves otherwise.

    The earlier rule asked whether a known-safe character preceded the root and treated every
    unenumerated shape as "not a path" — fail-open, and the candidate launcher's stderr format
    is not ours to control. These are the shapes that walked straight through it.
    """

    def _redact(self, text: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            _redact_probe_paths,
        )

        return _redact_probe_paths(text, Path("/nonexistent"))

    #: Everything that must never survive, in any shape below.
    _PRIVATE = ("Ada", "Smith", "Team", "Share", "srv", "private.yaml", "p.yaml", "Users")

    def _shapes(self) -> tuple:
        backslash, dquote, quote, backtick = chr(92), chr(34), chr(39), chr(96)
        posix = macos_home_path("Ada Smith", "private.yaml")
        drive = "C:" + backslash + backslash.join(("Users", "Ada Smith", "private.yaml"))
        unc = backslash * 2 + backslash.join(("srv", "Ada Share", "p.yaml"))
        return (
            # Delimiters the allowlist never enumerated.
            ("colon label, POSIX", f"config:{posix}: bad"),
            ("colon label, drive", f"config:{drive}: bad"),
            ("colon label, UNC", f"config:{unc}: bad"),
            ("backtick", f"error in {backtick}{posix}{backtick}: bad"),
            ("brace", "error in {" + posix + "}: bad"),
            ("pipe", f"error in |{posix}|: bad"),
            # Escaped same-quote: the naive rule closed at the escaped quote.
            ("escaped double quote",
             f'error in {dquote}/srv/Ada{backslash}{dquote}s Team/p.yaml{dquote}: bad'),
            ("escaped single quote",
             f"error in {quote}/srv/Ada{backslash}{quote}s Team/p.yaml{quote}: bad"),
            # And the shapes that already worked, kept as non-regressions.
            ("plain unquoted", f"error in {posix}: bad"),
            ("quoted with spaces", f"error in {quote}{posix}{quote}: bad"),
            ("drive unquoted", f"error in {drive}: bad"),
            ("UNC unquoted", f"error in {unc}: bad"),
        )

    def test_no_shape_leaks_a_private_tail(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            REDACTED_PROBE_PATH,
        )

        for label, text in self._shapes():
            with self.subTest(shape=label):
                out = self._redact(text)
                self.assertIn(REDACTED_PROBE_PATH, out)
                for leaked in self._PRIVATE:
                    self.assertNotIn(leaked, out, f"{label} leaked {leaked!r}: {out!r}")

    def test_relative_tokens_survive_by_positive_proof(self) -> None:
        # A relative token carries a parse reason and no location, so it stays byte for byte.
        # URLs no longer do (j#87837) — see R21ExternalUrlCollapseTest.
        for text in (
            "unknown key in relative/path.yaml",
            "expected one of ['down/right'] for split",
            "lane_placement.by_lane_kind must be a mapping",
        ):
            with self.subTest(text=text[:38]):
                self.assertEqual(self._redact(text), text)

    def test_a_url_after_a_private_path_does_not_rescue_the_path(self) -> None:
        # The guard skips a PROVEN-safe token; it must not skip anything before one.
        out = self._redact(
            "error in " + macos_home_path("Ada Smith", "p.yaml") + " see https://x.invalid/d"
        )
        for leaked in self._PRIVATE:
            self.assertNotIn(leaked, out)

    def test_the_real_public_exception_carries_no_private_tail(self) -> None:
        # End to end, with the scratch directory forced to contain a space.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            REDACTED_PROBE_PATH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Ada Team scratch"
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                "version: [1, 2\n", encoding="utf-8"
            )
            LaneLifecycleStore(home=root / "home").ensure_schema()
            launcher = _current_head_launcher(root)
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher, subprocess.run, 60.0, dict(os.environ),
                    repo_root=root / "repo", store_home=root / "home",
                )
            message = str(caught.exception)
            self.assertEqual(caught.exception.reason, TARGET_CONFIG_INVALID)
            for leaked in (str(root), "Ada Team scratch", "mozyo-config-parse-", str(Path.home())):
                self.assertNotIn(leaked, message)
            self.assertIn(REDACTED_PROBE_PATH, message)


class R18PrivacyFirstRedactionTest(unittest.TestCase):
    """R18: no private tail survives, in any of the shapes the contract covers."""

    def _redact(self, text: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            _redact_probe_paths,
        )

        return _redact_probe_paths(text, Path("/nonexistent"))

    def test_unquoted_paths_with_spaces_leave_no_tail(self) -> None:
        backslash = chr(92)
        cases = (
            ("POSIX", "error in " + macos_home_path("Private Team", "c.yaml") + ": bad"),
            ("drive", f"error in C:{backslash}Users{backslash}Ada Smith{backslash}c.yaml: x"),
            ("UNC", f"error in {backslash * 2}srv{backslash}Ada Share{backslash}c.yaml: x"),
        )
        for label, text in cases:
            with self.subTest(shape=label):
                out = self._redact(text)
                for leaked in ("Team", "Smith", "Share", "c.yaml", "Users"):
                    self.assertNotIn(leaked, out)
                # What comes BEFORE the path survives — that is where the reason sits.
                self.assertTrue(out.startswith("error in "))

    def test_a_quoted_path_may_contain_the_opposite_quote(self) -> None:
        backslash, dquote, quote = chr(92), chr(34), chr(39)
        cases = (
            ("double-quoted, apostrophe inside",
             f"error in {dquote}/srv/Ada{quote}s Team/c.yaml{dquote}: bad"),
            ("single-quoted, dquote inside",
             f"error in {quote}C:{backslash}Ada{dquote}s{backslash}c.yaml{quote}: bad"),
        )
        for label, text in cases:
            with self.subTest(shape=label):
                out = self._redact(text)
                for leaked in ("Ada", "Team", "srv", "c.yaml"):
                    self.assertNotIn(leaked, out)
                # The quoted rule is what makes this PRECISE rather than merely safe: the
                # closing quote terminates the path, so the trailing reason survives. Without
                # the same-quote split these fall through to the unquoted rule, which is still
                # leak-free but drops the rest of the line — so this assertion is what the
                # split actually buys, and what pins it.
                self.assertTrue(
                    out.endswith(": bad"),
                    f"text after the closing quote should survive: {out!r}",
                )

    def test_relative_tokens_are_still_intact(self) -> None:
        # URLs used to be listed here too. They are collapsed to a placeholder now
        # (j#87837) — see R21ExternalUrlCollapseTest.
        for text in (
            "unknown key in relative/path.yaml",
            "expected one of ['down/right'] for split",
        ):
            with self.subTest(text=text[:34]):
                self.assertEqual(self._redact(text), text)


class R14DeclaresNothingTest(unittest.TestCase):
    """R14: 'declares nothing' is the canonical loader's answer, not a whitespace test."""

    #: The full input class the contract claims, not just the easy member of it. Membership is
    #: the LOADER's answer, not an intuition about whitespace: a document containing a literal
    #: tab looked like "whitespace only" to me and is in fact a YAML scanner error, so it
    #: belongs below with the malformed ones. Delegating settled that instead of my guessing.
    _NOTHING = (
        "",
        "\n",
        "   \n   \n",
        "# operator note only\n",
        "#a\n#b\n",
        # Canonical YAML empty documents (design consultation j#87802 point 2). Each was
        # measured against the loader rather than assumed: these are the forms it resolves to
        # `RepoLocalConfig.default()`.
        "---\n",
        "--- \n",
        "---\n# c\n",
        "null\n",
        "~\n",
    )
    _SOMETHING = ("version: 2\n", "cli:\n  quiet: true\n")
    #: Documents that do not parse. Emphatically NOT "nothing" — they are real declarations
    #: this runtime cannot read, and admitting them would be the opposite of the contract.
    _MALFORMED = ("version: [1, 2\n", "   \n\t\n", "---\n---\n")

    def test_the_loader_agrees_these_declare_nothing(self) -> None:
        from mozyo_bridge.application.repo_local_config_loader import (
            RepoLocalConfigError,
            parses_as_default_config,
        )

        for text in self._NOTHING:
            with self.subTest(text=text[:20]):
                self.assertTrue(parses_as_default_config(text))
        for text in self._SOMETHING:
            with self.subTest(text=text[:20]):
                self.assertFalse(parses_as_default_config(text))
        for text in self._MALFORMED:
            with self.subTest(malformed=text[:20]):
                with self.assertRaises(RepoLocalConfigError):
                    parses_as_default_config(text)

    def test_both_call_sites_classify_them_identically(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            read_lane_target_config_text,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            read_target_config_text,
        )

        for text in self._NOTHING:
            with self.subTest(text=text[:20]):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / ".mozyo-bridge").mkdir()
                    (root / ".mozyo-bridge" / "config.yaml").write_text(text, encoding="utf-8")
                    # session-start path (reads the directory)
                    self.assertEqual(read_target_config_text(root)[0], "config_text_absent")
                    # committed-blob path (same document, different source)
                    state, _ = read_lane_target_config_text(
                        lambda **kw: ("blob_present", text),
                        base_commit="0" * 40,
                        lane_runtime_root="",
                        from_base_ref=True,
                    )
                    self.assertEqual(state, "config_text_absent")

    def test_a_comment_only_repo_admits_a_launcher_advertising_nothing(self) -> None:
        # The compatibility this restores: a repo that declares no schema must not require the
        # config-parse contract, because an older launcher reads it perfectly well.
        #
        # "advertising nothing" is scoped to the CONFIG-PARSE token — that is the carve-out
        # under test. The launcher therefore satisfies every other conjunct, including the
        # #14203 generation protocol: a launcher missing that one is refused whatever the
        # repo's config declares (it cannot emit the event the parent's finalize joins on),
        # so omitting it here would make this test pass or fail for the wrong axis.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                "# operator note only\n", encoding="utf-8"
            )
            LaneLifecycleStore(home=root / "home").ensure_schema()
            bare = root / "bare-mozyo-bridge"
            lines = "".join(
                f"printf '%s\\n' {line!r}\n"
                for line in (
                    f"usage: x [{_MARKER} NAME]",
                    "mozyo_attest_capability_schema=2",
                    "mozyo_attest_capability_stores=1_2",
                    "mozyo_attest_capability_lifecycle=1_2_3_4_5_6_7_8",
                    "mozyo_generation_protocol_capability=1",
                )
            )
            bare.write_text("#!/bin/sh\n" + lines + "exit 0\n", encoding="utf-8")
            bare.chmod(bare.stat().st_mode | stat.S_IEXEC)
            preflight_launcher_compatibility(
                str(bare), subprocess.run, _TIMEOUT, dict(os.environ),
                repo_root=root / "repo", store_home=root / "home",
            )

    def test_a_malformed_document_is_not_nothing(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            classify_config_text,
        )

        for text in self._MALFORMED:
            with self.subTest(text=text[:20]):
                state, kept = classify_config_text(text)
                self.assertEqual(state, "config_text_present")
                self.assertIsNotNone(kept)


class R15RedactionInputClassTest(unittest.TestCase):
    """R15: redact every absolute shape (quoted, spaced) and no relative token / URL."""

    def _redact(self, text: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            _redact_probe_paths,
        )

        return _redact_probe_paths(text, Path("/nonexistent"))

    def test_quoted_absolute_paths_with_spaces_are_redacted_whole(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            REDACTED_PROBE_PATH,
        )

        backslash = chr(92)
        drive = "C:" + backslash + backslash.join(("Users", "Ada Smith", "c.yaml"))
        unc = backslash * 2 + backslash.join(("server", "Ada Share", "c.yaml"))
        cases = (
            ("quoted POSIX", "error in '" + macos_home_path("Ada Team", "p", "c.yaml") + "': x"),
            ("quoted drive", f'error in "{drive}": x'),
            # UNC with a space (design consultation j#87802 point 3): the third root shape,
            # not just the two that were easy to write.
            ("quoted UNC", f'error in "{unc}": x'),
        )
        for label, text in cases:
            with self.subTest(shape=label):
                out = self._redact(text)
                self.assertIn(REDACTED_PROBE_PATH, out)
                # The tail after the space is exactly what the previous rule left behind.
                for leaked in ("Team", "Smith", "Share", "c.yaml"):
                    self.assertNotIn(leaked, out)

    def test_a_real_public_exception_carries_no_private_tail(self) -> None:
        """The end-to-end obligation, not just the pure helper (j#87802 point 3).

        The close condition constrains what an operator actually sees, so the assertion is
        made on the raised, public exception produced by the real conjunction over a real
        launcher — with the probe path forced to contain a space, which is the shape that
        previously survived redaction.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            REDACTED_PROBE_PATH,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Ada Team scratch"
            (root / "repo" / ".mozyo-bridge").mkdir(parents=True)
            # MALFORMED yaml, not an unknown key: a schema error names no file, so it could
            # not exercise redaction at all. A parser error names the document, which is the
            # path that reached the public error before this fix.
            (root / "repo" / ".mozyo-bridge" / "config.yaml").write_text(
                "version: [1, 2\n", encoding="utf-8"
            )
            LaneLifecycleStore(home=root / "home").ensure_schema()
            launcher = _current_head_launcher(root)
            with self.assertRaises(HerdrLauncherIncompatibleError) as caught:
                preflight_launcher_compatibility(
                    launcher, subprocess.run, 60.0, dict(os.environ),
                    repo_root=root / "repo", store_home=root / "home",
                )
            message = str(caught.exception)
            self.assertEqual(caught.exception.reason, TARGET_CONFIG_INVALID)
            for leaked in (str(root), "Ada Team scratch", "mozyo-config-parse-", str(Path.home())):
                self.assertNotIn(leaked, message)
            # The path was there to redact, and the parse reason survived it.
            self.assertIn(REDACTED_PROBE_PATH, message)
            self.assertIn("YAML", message)

    def test_relative_tokens_are_left_intact(self) -> None:
        for text in (
            "unknown key in relative/path.yaml",
            "expected one of ['down/right'] for split",
            "lane_placement.by_lane_kind must be a mapping",
        ):
            with self.subTest(text=text[:36]):
                self.assertEqual(self._redact(text), text)


class R12R16ContractTextConsistencyTest(unittest.TestCase):
    """R12 / R16: the retired declaration design must not survive anywhere in the subsystem.

    R12's pin looked for two literal strings in two modules, and R16 then found the retired
    design still stated in a third module, in a value type's docstring, and in this file's own
    header. A pin narrower than the claim it protects is how that happens, so the surface and
    the retired vocabulary are both enumerated here and swept together.
    """

    #: Every file that carries the target-authority contract. Adding a module to the
    #: subsystem without adding it here is the gap R16 reported, so the list is explicit.
    _SUBSYSTEM_FILES = (
        ("src", "mozyo_bridge", "e_140_adapter_provider", "f_130_terminal_runtime_provider",
         "application", "herdr_launcher_capability.py"),
        ("src", "mozyo_bridge", "e_140_adapter_provider", "f_130_terminal_runtime_provider",
         "application", "herdr_pane_lifecycle.py"),
        ("src", "mozyo_bridge", "e_110_execution_platform",
         "f_140_delegated_coordinator_nested_handoff", "application",
         "sublane_actuator_herdr_preflight.py"),
        ("vibes", "docs", "specs", "herdr-native-identity.md"),
        ("tests", "regressions", "test_issue_14258_launcher_target_compat.py"),
    )

    #: Tokens and phrasings of the DESIGN THAT WAS RETIRED by R4. Each was actually present at
    #: some point, so none is hypothetical — and each is COMPOSED rather than written, because
    #: this file is itself in the swept set and a literal here would make the sweep fail on its
    #: own definitions (the self-referential trap that makes a pointer test vacuous).
    @staticmethod
    def _retired_vocabulary() -> tuple:
        cap = "mozyo_attest_capability_config"
        return (
            cap + "=",
            cap + "_keys",
            "Both are " + "*declaration*" + " joins",
            "DECLARED" + " join",
            "declaration" + " rather than",
        )

    def test_no_retired_declaration_vocabulary_survives_in_the_subsystem(self) -> None:
        root = _SRC.parent
        for parts in self._SUBSYSTEM_FILES:
            path = root.joinpath(*parts)
            with self.subTest(path=parts[-1]):
                self.assertTrue(path.is_file(), f"{path} is not in the tree")
                text = path.read_text(encoding="utf-8")
                for retired in self._retired_vocabulary():
                    self.assertNotIn(
                        retired,
                        text,
                        f"{parts[-1]} still states the design R4 retired: {retired!r}",
                    )

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

    def test_the_lifecycle_value_type_no_longer_carries_config_key_state(self) -> None:
        # The retired design's residue in the type itself (R16): a field the config axis used
        # to fill and nothing sets any more.
        self.assertNotIn(
            "keys",
            {f.name for f in dataclasses.fields(TargetSchemaObservation)},
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
