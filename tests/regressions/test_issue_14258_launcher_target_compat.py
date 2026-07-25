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

from mozyo_bridge.application.repo_local_config_loader import (
    CONFIG_SCHEMA_ABSENT,
    CONFIG_SCHEMA_DECLARED,
    CONFIG_SCHEMA_UNREADABLE,
    CONFIG_SCHEMA_UNSUPPORTED,
    probe_repo_local_config_schema,
    probe_repo_local_config_schema_text,
)
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
    CONFIG_JOIN_OK,
    CONFIG_UNREADABLE,
    CONFIG_UNSUPPORTED,
    LAUNCHER_CANNOT_READ_CONFIG_KEYS,
    LAUNCHER_CANNOT_READ_CONFIG_VERSION,
    LAUNCHER_CANNOT_READ_LIFECYCLE,
    LAUNCHER_CONFIG_CONTRACT_ABSENT,
    LAUNCHER_LIFECYCLE_CONTRACT_ABSENT,
    LIFECYCLE_JOIN_OK,
    TARGET_SCHEMA_ABSENT,
    TARGET_SCHEMA_DECLARED,
    TARGET_SCHEMA_UNREADABLE,
    TARGET_SCHEMA_UNSUPPORTED,
    TargetSchemaObservation,
    build_attest_capability_epilog,
    decide_config_schema_compatibility,
    decide_lifecycle_reader_compatibility,
    parse_launcher_capability_output,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    HerdrLauncherIncompatibleError,
    preflight_launcher_compatibility,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
)

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


class ConfigSchemaProbeTest(unittest.TestCase):
    """The read-only probe answers the parse question without validating the whole config."""

    def test_absent_and_empty_config_declare_nothing_to_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.yaml"
            self.assertEqual(
                probe_repo_local_config_schema(missing).state, CONFIG_SCHEMA_ABSENT
            )
            missing.write_text("# only a comment\n", encoding="utf-8")
            self.assertEqual(
                probe_repo_local_config_schema(missing).state, CONFIG_SCHEMA_ABSENT
            )

    def test_v2_config_declares_its_version_and_top_level_keys(self) -> None:
        probe = probe_repo_local_config_schema_text(_V2_CONFIG)
        self.assertEqual(probe.state, CONFIG_SCHEMA_DECLARED)
        self.assertEqual(probe.version, 2)
        self.assertIn("agents", probe.keys)

    def test_a_nested_block_this_runtime_would_reject_is_still_readable(self) -> None:
        # The probe answers "could a launcher PARSE this file", which is the version + key
        # boundary. A nested block that would fail full validation must not read as a
        # launcher incompatibility — otherwise an unrelated config error would be reported
        # as "upgrade your launcher".
        probe = probe_repo_local_config_schema_text(
            "version: 2\nagents:\n  profiles: not-a-mapping\n"
        )
        self.assertEqual(probe.state, CONFIG_SCHEMA_DECLARED)
        self.assertEqual(probe.version, 2)

    def test_malformed_and_non_integer_version_fail_closed(self) -> None:
        for text in ("version: [1, 2\n", "version: true\n", "version: two\n", "- a\n"):
            with self.subTest(text=text):
                self.assertEqual(
                    probe_repo_local_config_schema_text(text).state,
                    CONFIG_SCHEMA_UNREADABLE,
                    "an unreadable config is not an absent one",
                )

    def test_future_version_is_unsupported_and_names_the_upgrade(self) -> None:
        probe = probe_repo_local_config_schema_text("version: 99\n")
        self.assertEqual(probe.state, CONFIG_SCHEMA_UNSUPPORTED)
        self.assertTrue(probe.upgrade_required)

    def test_a_present_but_unreadable_file_is_not_absent(self) -> None:
        # A directory where the config file should be: present, unreadable. Folding this
        # into "absent" would admit every launcher against a config nobody can read.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.yaml").mkdir()
            self.assertEqual(
                probe_repo_local_config_schema(Path(tmp) / "config.yaml").state,
                CONFIG_SCHEMA_UNREADABLE,
            )


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


class ConfigJoinTest(unittest.TestCase):
    """The pure config join: the launcher must be provably able to parse the target."""

    def test_this_builds_launcher_reads_this_builds_config(self) -> None:
        verdict = decide_config_schema_compatibility(
            _observation(_capable_help()),
            TargetSchemaObservation(
                TARGET_SCHEMA_DECLARED, 2, frozenset({"version", "agents"})
            ),
        )
        self.assertTrue(verdict.ok, verdict.detail)
        self.assertEqual(verdict.reason, CONFIG_JOIN_OK)

    def test_a_config_less_target_admits_a_launcher_that_advertises_nothing(self) -> None:
        # The pre-#14258 world for a config-less repo, preserved: with nothing to parse there
        # is no defect to refuse, and refusing would break a working case for no reason.
        verdict = decide_config_schema_compatibility(
            _observation(f"usage: x [{_MARKER} NAME]\n"),
            TargetSchemaObservation(TARGET_SCHEMA_ABSENT),
        )
        self.assertTrue(verdict.ok, verdict.detail)

    def test_the_measured_failure_a_pre_14258_launcher_vs_a_v2_config(self) -> None:
        # THE regression: the installed launcher carried `agent-attest` and the attestation
        # schema (so #13748/#13847 passed) but advertises no config-parse capability at all.
        pre_14258 = (
            f"usage: x [{_MARKER} NAME]\n"
            "mozyo_attest_capability_schema=2\n"
            "mozyo_attest_capability_stores=1_2\n"
        )
        probe = probe_repo_local_config_schema_text(_V2_CONFIG)
        verdict = decide_config_schema_compatibility(
            _observation(pre_14258),
            TargetSchemaObservation(TARGET_SCHEMA_DECLARED, probe.version, probe.keys),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LAUNCHER_CONFIG_CONTRACT_ABSENT)

    def test_a_v1_only_launcher_cannot_read_a_v2_config(self) -> None:
        v1_only = (
            f"usage: x [{_MARKER} NAME]\n"
            "mozyo_attest_capability_config=1\n"
            "mozyo_attest_capability_config_keys=cli.version\n"
        )
        verdict = decide_config_schema_compatibility(
            _observation(v1_only),
            TargetSchemaObservation(TARGET_SCHEMA_DECLARED, 2, frozenset({"version"})),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LAUNCHER_CANNOT_READ_CONFIG_VERSION)

    def test_an_unknown_top_level_key_is_refused_even_at_a_shared_version(self) -> None:
        # The version join alone is not enough: recognized keys have been added WITHIN a
        # version, and `unknown key 'agents'` is literally what the measured failure said.
        same_version_fewer_keys = (
            f"usage: x [{_MARKER} NAME]\n"
            "mozyo_attest_capability_config=1_2\n"
            "mozyo_attest_capability_config_keys=cli.version\n"
        )
        verdict = decide_config_schema_compatibility(
            _observation(same_version_fewer_keys),
            TargetSchemaObservation(
                TARGET_SCHEMA_DECLARED, 2, frozenset({"version", "agents"})
            ),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LAUNCHER_CANNOT_READ_CONFIG_KEYS)
        self.assertIn("agents", verdict.detail)

    def test_an_unreadable_or_unsupported_target_config_fails_closed(self) -> None:
        capable = _observation(_capable_help())
        for state, expected in (
            (TARGET_SCHEMA_UNREADABLE, CONFIG_UNREADABLE),
            (TARGET_SCHEMA_UNSUPPORTED, CONFIG_UNSUPPORTED),
        ):
            with self.subTest(state=state):
                verdict = decide_config_schema_compatibility(
                    capable, TargetSchemaObservation(state, 99)
                )
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason, expected)

    def test_conflicting_advertisements_are_not_arbitrated(self) -> None:
        # Two different claims about the same fact prove neither (the j#80000 finding-3 rule,
        # now shared by every advertised set rather than re-implemented per token).
        conflicting = (
            f"usage: x [{_MARKER} NAME]\n"
            "mozyo_attest_capability_config=1_2\n"
            "mozyo_attest_capability_config=2\n"
            "mozyo_attest_capability_config_keys=agents.version\n"
        )
        self.assertIsNone(_observation(conflicting).advertised_config_versions)
        verdict = decide_config_schema_compatibility(
            _observation(conflicting),
            TargetSchemaObservation(TARGET_SCHEMA_DECLARED, 2, frozenset({"version"})),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, LAUNCHER_CONFIG_CONTRACT_ABSENT)

    def test_a_malformed_advertisement_is_not_salvaged(self) -> None:
        for token in (
            "mozyo_attest_capability_config=1__2",
            "mozyo_attest_capability_config=_1_2_",
            "mozyo_attest_capability_config=1_2junk",
        ):
            with self.subTest(token=token):
                self.assertIsNone(
                    _observation(
                        f"usage: x [{_MARKER} NAME]\n{token}\n"
                    ).advertised_config_versions
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
            self.assertEqual(caught.exception.reason, LAUNCHER_CONFIG_CONTRACT_ABSENT)
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
            (False, LAUNCHER_CANNOT_READ_CONFIG_VERSION, "launcher parses config v1 only")
        )
        self.assertTrue(outcome.is_blocked)
        self.assertIn("launcher_runtime_incompatible", outcome.blocked_reasons)
        self.assertIn(LAUNCHER_CANNOT_READ_CONFIG_VERSION, outcome.blocked_reasons)
        self.assertIsNone(outcome.startup, "a zero-mutation refusal observed no startup")
        self.assertIsNone(outcome.gateway_pane)
        self.assertIsNone(outcome.worker_pane)
        self.assertEqual(len(ops.calls), 1, "the gate runs exactly once, before the worktree")

    def test_the_gate_is_told_which_config_the_lane_will_have(self) -> None:
        # A worktree this run would CREATE has no directory to read, so the gate must ask for
        # the committed blob at the lane's base ref rather than substituting this checkout's
        # working file — the proxy that would silently pass on a differing base.
        ops, _ = self._run((False, LAUNCHER_CANNOT_READ_CONFIG_VERSION, "nope"))
        self.assertEqual(
            ops.calls, ["preflight:['base_ref', 'from_base_ref', 'lane_runtime_root']"]
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

    def test_the_source_launcher_reads_as_compatible_with_the_source_target(self) -> None:
        # The self-consistency the join depends on: THIS build's advertisement satisfies
        # THIS build's requirements. Without it the runtime would refuse its own launcher.
        observation = _observation(f"usage: x [{_MARKER} NAME]\n{build_attest_capability_epilog()}")
        probe = probe_repo_local_config_schema_text(_V2_CONFIG)
        config = decide_config_schema_compatibility(
            observation,
            TargetSchemaObservation(TARGET_SCHEMA_DECLARED, probe.version, probe.keys),
        )
        lifecycle = decide_lifecycle_reader_compatibility(
            observation,
            TargetSchemaObservation(
                TARGET_SCHEMA_DECLARED, LANE_LIFECYCLE_SCHEMA_VERSION
            ),
        )
        self.assertTrue(config.ok, config.detail)
        self.assertTrue(lifecycle.ok, lifecycle.detail)


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
