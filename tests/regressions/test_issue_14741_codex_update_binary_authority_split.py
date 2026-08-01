"""Redmine #14741 — Codex update prompt mis-read as a ready composer, and the managed
exec / provider-updater authority split behind the sublane gateway's clean-exit loop.

Every test here is deterministic: the provider is a **fake** — synthetic profiles and
real-but-inert executable files in a tmp tree — so nothing depends on an installed Codex,
on a network update, or on which package manager owns the host. The one thing that could
only come from the shipped binary (the update screen's rendered strings) is pinned as
data in `agent_provider_profiles.yaml` and asserted here against the captured render, not
re-derived at test time.

Live provenance of the pinned pane text (codex-cli 0.146.0, aarch64-apple-darwin), read
from the shipped binary and confirmed by rendering the real screen into a disposable,
credential-free CODEX_HOME with zero keystrokes sent:

    ✨ Update available!  0.146.0 -> 99.0.0
    Release notes: https://github.com/openai/codex/releases/latest
    › 1. Update now (runs `npm install -g @openai/codex`)
      2. Skip
      3. Skip until next version
    Press enter to continue
"""

from __future__ import annotations

import os
import stat
import tempfile
import types
import unittest

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    ATTESTATION_OK,
    AUTHORITY_ALIGNED,
    AUTHORITY_NOT_EVALUATED,
    AUTHORITY_SPLIT,
    AUTHORITY_UNKNOWN,
    BINDING_DRIFTED,
    BINDING_MATCHED,
    BINDING_NOT_EVALUATED,
    BINDING_UNKNOWN,
    EVIDENCE_UNAVAILABLE,
    HEALTH_DETAIL,
    HEALTH_EXECUTABLE_BINDING_DRIFT,
    HEALTH_HEALTHY,
    HEALTH_INVENTORY_UNREADABLE,
    HEALTH_OUTCOMES,
    HEALTH_PROVIDER_EXITED,
    HEALTH_STARTUP_EVIDENCE_UNAVAILABLE,
    HEALTH_STARTUP_INTERACTION,
    HEALTH_UPDATE_AUTHORITY_SPLIT,
    HEALTH_UPDATE_AUTHORITY_UNVERIFIED,
    SCREEN_BLOCKED,
    SCREEN_CLEAR,
    classify_startup_health,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
    ADMISSION_ADMITTED,
    ADMISSION_BLOCKED,
    evaluate_startup_admission,
    startup_admission_record_lines,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_update_authority_preflight import (  # noqa: E501
    evaluate_update_authority,
    executable_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
    AGENT_PROVIDER_PROFILES,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
    REASON_IDENTITY_UNCORRESPONDED,
    REASON_OK,
    REASON_PROVIDER_UNREGISTERED,
    REASON_QUERY_EXECUTABLE_UNRESOLVED,
    REASON_QUERY_FAILED,
    UpdaterTargetResolution,
    resolve_updater_target,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
    AgentProviderProfile,
    AgentProviderProfileRegistry,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_update_authority import (  # noqa: E501
    AUTHORITY_DETAIL,
    BINDING_DETAIL,
    UPDATE_AUTHORITIES,
    EXECUTABLE_BINDINGS,
    UpdateAuthority,
    UpdateAuthorityError,
    classify_executable_binding,
    classify_update_authority,
)

# The exact visible text of the live-captured Codex update prompt. Wrapped and framed the
# way a pane renders it, to prove the classifier's folding survives the framing.
CAPTURED_UPDATE_PROMPT = (
    "  ✨ Update available!0.146.0 -> 99.0.0"
    "Release notes: https://github.com/openai/codex/releases/latest"
    "› 1. Update now (runs `npm install -g @openai/codex`)"
    "2.Skip3.SkipuntilnextversionPress enter to continue"
)

# The updater actually running — the screen the live pane showed while npm install ran.
CAPTURED_UPDATE_IN_PROGRESS = "Updating Codex via `npm install -g @openai/codex`..."

# A positive control: a real, ready Codex composer must stay admitted. A gate that
# blocks every pane is not a fix, it is a different outage.
READY_COMPOSER = (
    "╭──────────╮\n"
    "│ > Try \"edit config.py to add a flag\"                 │\n"
    "╰──────────╯\n"
    "  ? for shortcuts                          98% context left"
)


def _fake_profile_registry(provider_id: str = "fakex") -> AgentProviderProfileRegistry:
    """A synthetic single-provider registry — never the packaged Codex profile."""
    profile = AgentProviderProfile.from_record(
        provider_id,
        {
            "protocol": "interactive_cli_tui",
            "executable": {
                "command": "fakex",
                "env_override": "MOZYO_AGENT_FAKEX_BINARY",
            },
            "discovery_aliases": [provider_id],
            "process_names": [provider_id],
            "capabilities": ["interactive_tui", "launch_argv_override"],
            "managed_flags": {},
        },
        schema_version="3",
    )
    registry = AgentProviderProfileRegistry()
    registry.register(profile)
    return registry


def _make_executable(directory: str, name: str = "fakex") -> str:
    """A real, inert executable file. Never run — only resolved and stat'd."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
        # Deliberately never invoked: the resolver only stats it.
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return os.path.realpath(path)


class CodexUpdateScreenIsAStartupBlockerTest(unittest.TestCase):
    """Acceptance 1 — the update screens are declared, and refuse the send at zero bytes."""

    def setUp(self) -> None:
        self.profile = AGENT_PROVIDER_PROFILES.require("codex")

    def test_captured_update_prompt_matches_the_declared_blocker(self) -> None:
        blocker = self.profile.match_startup_blocker(CAPTURED_UPDATE_PROMPT)
        self.assertIsNotNone(
            blocker,
            "the live-captured Codex update prompt must classify as a startup blocker; "
            "this exact screen consumed an Implementation Request (#14741)",
        )
        self.assertEqual(blocker.blocker_id, "update_prompt_available")

    def test_captured_update_in_progress_matches_the_declared_blocker(self) -> None:
        blocker = self.profile.match_startup_blocker(CAPTURED_UPDATE_IN_PROGRESS)
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.blocker_id, "update_in_progress")

    def test_ready_composer_is_not_blocked(self) -> None:
        """The positive control. A false positive here is an outage, not a guard."""
        self.assertIsNone(self.profile.match_startup_blocker(READY_COMPOSER))

    def test_lone_generic_phrase_does_not_false_positive(self) -> None:
        """Each blocker is an AND of co-located signatures, never an any-match."""
        for lone in (
            "Update available!",
            "Update now (runs",
            "Updating Codex via",
            "@openai/codex",
            # A worker legitimately discussing an update must not block its own lane.
            "I will check whether an update is available for the CLI.",
        ):
            with self.subTest(lone=lone):
                self.assertIsNone(self.profile.match_startup_blocker(lone))

    def test_blocker_ids_are_the_only_thing_that_leaves_the_screen(self) -> None:
        """No pane text, path, or version may be reachable from the declared data."""
        for blocker in self.profile.startup_blockers:
            self.assertTrue(blocker.blocker_id.isidentifier())
            self.assertNotIn("/", blocker.blocker_id)

    def test_claude_blockers_are_unchanged(self) -> None:
        """#14741 adds Codex screens; it must not perturb the #13760 Claude set."""
        claude = AGENT_PROVIDER_PROFILES.require("claude")
        self.assertEqual(
            [b.blocker_id for b in claude.startup_blockers],
            [
                "workspace_trust_confirmation",
                "directory_trust_confirmation",
                "first_run_theme",
                "login_required",
            ],
        )


class UpdatePromptRefusesTheSendAtZeroBytesTest(unittest.TestCase):
    """Acceptance 1 end-to-end: the pre-send gate refuses, and sends nothing at all.

    The gate itself is provider-neutral (#13760) — these tests prove the new Codex data
    actually reaches it, which is the difference between a declared screen and a closed
    hole. The read primitive is a counting fake, so "zero send" is asserted rather than
    assumed: the receiver is read exactly once and never written to.
    """

    def _admit(self, content: str):
        reads = []

        def read_visible():
            reads.append(content)
            return content

        admission = evaluate_startup_admission(
            provider_id="codex", read_visible=read_visible
        )
        return admission, reads

    def test_update_prompt_is_blocked_and_names_only_the_blocker_id(self) -> None:
        admission, reads = self._admit(CAPTURED_UPDATE_PROMPT)
        self.assertEqual(admission.outcome, ADMISSION_BLOCKED)
        self.assertFalse(admission.admitted)
        self.assertEqual(admission.blocker_id, "update_prompt_available")
        self.assertEqual(len(reads), 1, "the receiver is read once, at action time")
        # The screen's own text must never reach a structured outcome / durable record.
        payload = admission.to_telemetry_dict()
        for value in payload.values():
            self.assertNotIn("Update available", str(value))
            self.assertNotIn("npm install", str(value))

    def test_update_in_progress_is_blocked(self) -> None:
        admission, _ = self._admit(CAPTURED_UPDATE_IN_PROGRESS)
        self.assertEqual(admission.outcome, ADMISSION_BLOCKED)
        self.assertEqual(admission.blocker_id, "update_in_progress")

    def test_ready_codex_composer_is_still_admitted(self) -> None:
        """Positive control end-to-end: an admitted send stays byte-identical."""
        admission, _ = self._admit(READY_COMPOSER)
        self.assertEqual(admission.outcome, ADMISSION_ADMITTED)
        self.assertTrue(admission.admitted)

    def test_refusal_telemetry_carries_no_pane_text(self) -> None:
        admission, _ = self._admit(CAPTURED_UPDATE_PROMPT)
        lines = startup_admission_record_lines(admission)
        self.assertTrue(lines)
        joined = "\n".join(lines)
        self.assertIn("update_prompt_available", joined)
        for leaked in ("Update available", "npm install", "@openai/codex", "0.146.0"):
            self.assertNotIn(leaked, joined)


class UpdateAuthorityClassifierTest(unittest.TestCase):
    """Acceptance 2 — the split is decided, or explicitly not decided. Never guessed."""

    def test_aligned_when_the_single_path_target_is_the_managed_one(self) -> None:
        self.assertEqual(
            classify_update_authority(
                exec_target="/opt/pm/lib/node_modules/x/bin/x",
                updater_write_roots=("/opt/pm/lib/node_modules/x/bin/x",),
                updater_roots_readable=True,
            ),
            AUTHORITY_ALIGNED,
        )

    def test_split_when_the_updater_reaches_a_different_install(self) -> None:
        """The measured #14741 shape: override pinned one install, PATH held another."""
        self.assertEqual(
            classify_update_authority(
                exec_target="/opt/os/lib/node_modules/x/bin/x",
                updater_write_roots=("/home/u/.nvm/versions/node/v22/bin/x",),
                updater_roots_readable=True,
            ),
            AUTHORITY_SPLIT,
        )

    def test_split_when_the_updater_target_is_itself_ambiguous(self) -> None:
        self.assertEqual(
            classify_update_authority(
                exec_target="/opt/a/x",
                updater_write_roots=("/opt/a/x", "/opt/b/x"),
                updater_roots_readable=True,
            ),
            AUTHORITY_SPLIT,
        )

    def test_unreadable_path_is_unknown_never_aligned(self) -> None:
        self.assertEqual(
            classify_update_authority(
                exec_target="/opt/a/x", updater_write_roots=(), updater_roots_readable=False
            ),
            AUTHORITY_UNKNOWN,
        )

    def test_no_path_resolution_is_unknown_never_aligned(self) -> None:
        """"The updater has nowhere to write" is a guess, not an observation."""
        self.assertEqual(
            classify_update_authority(
                exec_target="/opt/a/x", updater_write_roots=(), updater_roots_readable=True
            ),
            AUTHORITY_UNKNOWN,
        )

    def test_missing_exec_target_is_unknown(self) -> None:
        for empty in ("", "   "):
            with self.subTest(empty=empty):
                self.assertEqual(
                    classify_update_authority(
                        exec_target=empty,
                        updater_write_roots=("/opt/a/x",),
                        updater_roots_readable=True,
                    ),
                    AUTHORITY_UNKNOWN,
                )

    def test_duplicate_path_entries_collapse_rather_than_read_as_ambiguous(self) -> None:
        self.assertEqual(
            classify_update_authority(
                exec_target="/opt/a/x",
                updater_write_roots=("/opt/a/x", "/opt/a/x", ""),
                updater_roots_readable=True,
            ),
            AUTHORITY_ALIGNED,
        )

    def test_classifier_is_total_over_every_documented_input(self) -> None:
        """No input shape may raise: a gate that raises cannot fail closed."""
        for readable in (True, False):
            for targets in ((), ("/a",), ("/a", "/b"), (None, 1, "/a")):
                for target in ("", "/a", None):
                    with self.subTest(r=readable, t=targets, x=target):
                        verdict = classify_update_authority(
                            exec_target=target,
                            updater_write_roots=targets,
                            updater_roots_readable=readable,
                        )
                        self.assertIn(verdict, UPDATE_AUTHORITIES)


class ExecutableBindingClassifierTest(unittest.TestCase):
    """Acceptance 3 — same-version reinstall is a match; a rewrite is a drift."""

    def test_same_version_reinstall_is_a_match_not_a_drift(self) -> None:
        """Nothing this lane runs changed, so a reinstall must not flap the lane."""
        identity = executable_identity("/opt/a/x", "0.146.0")
        self.assertEqual(
            classify_executable_binding(
                bound_identity=identity, observed_identity=identity
            ),
            BINDING_MATCHED,
        )

    def test_version_change_at_the_same_path_is_a_drift(self) -> None:
        """An in-place package-manager rewrite: path pinning alone cannot see it."""
        self.assertEqual(
            classify_executable_binding(
                bound_identity=executable_identity("/opt/a/x", "0.145.0"),
                observed_identity=executable_identity("/opt/a/x", "0.146.0"),
            ),
            BINDING_DRIFTED,
        )

    def test_same_version_at_a_different_path_is_a_drift(self) -> None:
        """The authority split's other face: right version, wrong install."""
        self.assertEqual(
            classify_executable_binding(
                bound_identity=executable_identity("/opt/a/x", "0.146.0"),
                observed_identity=executable_identity("/opt/b/x", "0.146.0"),
            ),
            BINDING_DRIFTED,
        )

    def test_unobserved_identity_is_unknown_never_matched(self) -> None:
        self.assertEqual(
            classify_executable_binding(
                bound_identity=executable_identity("/opt/a/x", "0.146.0"),
                observed_identity="",
            ),
            BINDING_UNKNOWN,
        )

    def test_no_binding_supplied_is_not_evaluated(self) -> None:
        self.assertEqual(
            classify_executable_binding(bound_identity="", observed_identity="/opt/a/x@1"),
            BINDING_NOT_EVALUATED,
        )

    def test_incomplete_identity_is_no_identity(self) -> None:
        """Half an identity is not a weaker pin; it is the absence of one."""
        self.assertEqual(executable_identity("/opt/a/x", ""), "")
        self.assertEqual(executable_identity("", "0.146.0"), "")
        self.assertEqual(executable_identity("  ", "  "), "")

    def test_classifier_is_total(self) -> None:
        for bound in ("", "  ", "a", None, 3):
            for observed in ("", "  ", "a", None, 3):
                with self.subTest(b=bound, o=observed):
                    self.assertIn(
                        classify_executable_binding(
                            bound_identity=bound, observed_identity=observed
                        ),
                        EXECUTABLE_BINDINGS,
                    )


class UpdateAuthorityRecordTest(unittest.TestCase):
    """The record is closed, and it carries nothing about the host."""

    def test_admits_actuation_only_for_evaluated_or_unevaluated_positives(self) -> None:
        cases = {
            (AUTHORITY_NOT_EVALUATED, BINDING_NOT_EVALUATED): True,
            (AUTHORITY_ALIGNED, BINDING_MATCHED): True,
            (AUTHORITY_ALIGNED, BINDING_NOT_EVALUATED): True,
            (AUTHORITY_SPLIT, BINDING_MATCHED): False,
            (AUTHORITY_UNKNOWN, BINDING_MATCHED): False,
            (AUTHORITY_ALIGNED, BINDING_DRIFTED): False,
            (AUTHORITY_ALIGNED, BINDING_UNKNOWN): False,
        }
        for (authority, binding), admits in cases.items():
            with self.subTest(a=authority, b=binding):
                record = UpdateAuthority(
                    provider="fakex", authority=authority, binding=binding
                )
                self.assertEqual(record.admits_actuation, admits)

    def test_payload_carries_no_path_version_or_env_value(self) -> None:
        payload = UpdateAuthority(
            provider="fakex",
            authority=AUTHORITY_SPLIT,
            binding=BINDING_DRIFTED,
            updater_targets=2,
        ).as_payload()
        self.assertEqual(
            set(payload), {"provider", "authority", "binding", "updater_targets"}
        )
        for value in payload.values():
            self.assertNotIn("/", str(value))

    def test_unknown_tokens_and_bad_counts_fail_closed(self) -> None:
        for kwargs in (
            {"authority": "probably_fine"},
            {"binding": "probably_fine"},
            {"updater_targets": -1},
            {"updater_targets": True},
            {"provider": ""},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(UpdateAuthorityError):
                    UpdateAuthority(**{"provider": "fakex", **kwargs})

    def test_every_token_has_a_fixed_operator_sentence(self) -> None:
        self.assertEqual(set(AUTHORITY_DETAIL), UPDATE_AUTHORITIES)
        self.assertEqual(set(BINDING_DETAIL), EXECUTABLE_BINDINGS)


class UpdateAuthorityPreflightTest(unittest.TestCase):
    """Acceptance 2/3 at action time, against real files in a tmp tree — no live provider.

    Redmine #14741 review j#95741 F2: the updater's write target is **supplied by a
    probe**, never inferred from where the provider's command sits on PATH. These tests
    pin that distinction directly, because the first cut's PATH inference is exactly what
    promoted an unverified authority to a positive `aligned`.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.registry = _fake_profile_registry()

    def _probe(self, *roots, resolved=True):
        return lambda provider_id: (list(roots), resolved)

    def test_no_probe_is_not_evaluated_never_unknown(self) -> None:
        """D2 item 1: an unarmed caller means the gate does not apply, not that it failed."""
        bindir = os.path.join(self.root, "os", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority("fakex", env, registry=self.registry)
        self.assertEqual(record.authority, AUTHORITY_NOT_EVALUATED)
        self.assertTrue(
            record.admits_actuation,
            "unarmed is byte-invariant with pre-#14741; promoting it to unknown is what "
            "refused every Claude send on every host (j#96202)",
        )
        # But an ARMED caller that cannot resolve is still zero-actuation.
        armed = evaluate_update_authority(
            "fakex", env, registry=self.registry, updater_targets=lambda pid: ((), False)
        )
        self.assertEqual(armed.authority, AUTHORITY_UNKNOWN)
        self.assertFalse(armed.admits_actuation)

    def test_provider_command_on_path_is_no_longer_evidence_of_alignment(self) -> None:
        """The j#95741 F2 regression, stated as the property that was violated.

        One `fakex` on PATH, identical to the override — the exact configuration the
        first cut called `aligned`. Without a resolved updater target it must be
        `unknown`: where a binary sits on PATH and where its package manager writes are
        independently determined facts.
        """
        bindir = os.path.join(self.root, "os", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority("fakex", env, registry=self.registry)
        self.assertNotEqual(
            record.authority,
            AUTHORITY_ALIGNED,
            "a single matching PATH entry is a proxy, not the updater's write target",
        )

    def test_aligned_only_when_a_resolved_updater_root_contains_the_exec_target(self) -> None:
        pkg_root = os.path.join(self.root, "pm", "lib", "node_modules")
        bindir = os.path.join(pkg_root, "provider", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority(
            "fakex", env, registry=self.registry, updater_targets=self._probe(pkg_root)
        )
        self.assertEqual(record.authority, AUTHORITY_ALIGNED)
        self.assertTrue(record.admits_actuation)

    def test_split_when_the_updater_writes_to_another_prefix(self) -> None:
        """The measured #14741 shape: the override pins one install, the package manager
        owns another — and the second one need not exist yet."""
        pinned = _make_executable(os.path.join(self.root, "os", "bin"))
        nvm_root = os.path.join(self.root, "nvm", "lib", "node_modules")
        os.makedirs(nvm_root, exist_ok=True)
        env = {
            "PATH": os.path.join(self.root, "os", "bin"),
            "MOZYO_AGENT_FAKEX_BINARY": pinned,
        }
        record = evaluate_update_authority(
            "fakex", env, registry=self.registry, updater_targets=self._probe(nvm_root)
        )
        self.assertEqual(record.authority, AUTHORITY_SPLIT)
        self.assertFalse(
            record.admits_actuation,
            "a demonstrated split must refuse the send: updating cannot fix this lane",
        )

    def test_sibling_prefix_is_not_read_as_containment(self) -> None:
        """`/a/nodes` must not count as inside `/a/node`. A false alignment is the one
        direction this classifier may never fail in."""
        near = os.path.join(self.root, "nodes")
        root = os.path.join(self.root, "node")
        os.makedirs(root, exist_ok=True)
        pinned = _make_executable(os.path.join(near, "bin"))
        env = {"PATH": os.path.join(near, "bin"), "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority(
            "fakex", env, registry=self.registry, updater_targets=self._probe(root)
        )
        self.assertEqual(record.authority, AUTHORITY_SPLIT)

    def test_ambiguous_updater_target_is_split(self) -> None:
        pkg_root = os.path.join(self.root, "pm", "lib", "node_modules")
        bindir = os.path.join(pkg_root, "provider", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority(
            "fakex",
            env,
            registry=self.registry,
            updater_targets=self._probe(pkg_root, os.path.join(self.root, "other")),
        )
        self.assertEqual(record.authority, AUTHORITY_SPLIT)

    def test_probe_that_fails_or_raises_is_unknown_not_aligned(self) -> None:
        bindir = os.path.join(self.root, "os", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}

        def raising(_provider_id):
            raise RuntimeError("package manager unavailable")

        for probe in (raising, self._probe(bindir, resolved=False)):
            with self.subTest(probe=probe):
                record = evaluate_update_authority(
                    "fakex", env, registry=self.registry, updater_targets=probe
                )
                self.assertEqual(record.authority, AUTHORITY_UNKNOWN)
                self.assertFalse(record.admits_actuation)

    def test_symlinked_alias_is_not_a_false_split(self) -> None:
        """Both sides are realpath-resolved, so an alias and its target compare equal."""
        pkg_root = os.path.join(self.root, "pkg")
        real_dir = os.path.join(pkg_root, "bin")
        target = _make_executable(real_dir)
        link_dir = os.path.join(self.root, "shim")
        os.makedirs(link_dir, exist_ok=True)
        link = os.path.join(link_dir, "fakex")
        os.symlink(target, link)
        env = {"PATH": link_dir, "MOZYO_AGENT_FAKEX_BINARY": link}
        record = evaluate_update_authority(
            "fakex", env, registry=self.registry, updater_targets=self._probe(pkg_root)
        )
        self.assertEqual(record.authority, AUTHORITY_ALIGNED)

    def test_unsafe_path_is_unknown_and_does_not_raise(self) -> None:
        """An unsafe PATH breaks the provider resolution itself -> unknown, never a raise.

        No override here on purpose: with one, the launch resolves regardless of PATH
        safety, and PATH safety is then the *manager* resolver's concern, which
        `D2EffectiveManagerResolutionTest` pins separately.
        """
        _make_executable(os.path.join(self.root, "os", "bin"))
        env = {"PATH": os.pathsep.join([os.path.join(self.root, "os", "bin"), "relative/dir"])}
        record = evaluate_update_authority(
            "fakex",
            env,
            registry=self.registry,
            updater_targets=lambda pid: ((), False),
        )
        self.assertEqual(record.authority, AUTHORITY_UNKNOWN)
        self.assertFalse(record.admits_actuation)

    def test_unresolvable_provider_is_unknown_and_does_not_raise(self) -> None:
        record = evaluate_update_authority(
            "fakex",
            {"PATH": os.path.join(self.root, "empty")},
            registry=self.registry,
            updater_targets=lambda pid: ((), False),
        )
        self.assertEqual(record.authority, AUTHORITY_UNKNOWN)
        self.assertFalse(record.admits_actuation)

    def test_unknown_provider_is_unknown_and_does_not_raise(self) -> None:
        record = evaluate_update_authority(
            "no_such_provider",
            {"PATH": self.root},
            registry=self.registry,
            updater_targets=lambda pid: ((), False),
        )
        self.assertEqual(record.authority, AUTHORITY_UNKNOWN)

    def test_binding_drift_is_a_demonstrated_wrong_binary(self) -> None:
        pkg_root = os.path.join(self.root, "pm")
        bindir = os.path.join(pkg_root, "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority(
            "fakex",
            env,
            registry=self.registry,
            updater_targets=self._probe(pkg_root),
            bound_identity=executable_identity(pinned, "0.145.0"),
            observed_identity=executable_identity(pinned, "0.146.0"),
        )
        self.assertEqual(record.authority, AUTHORITY_ALIGNED)
        self.assertEqual(record.binding, BINDING_DRIFTED)
        self.assertFalse(record.admits_actuation)

    def test_same_version_reinstall_stays_admitted(self) -> None:
        """Nothing this lane runs changed, so a reinstall must not fence the lane off."""
        pkg_root = os.path.join(self.root, "pm")
        bindir = os.path.join(pkg_root, "bin")
        pinned = _make_executable(bindir)
        identity = executable_identity(pinned, "0.146.0")
        record = evaluate_update_authority(
            "fakex",
            {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned},
            registry=self.registry,
            updater_targets=self._probe(pkg_root),
            bound_identity=identity,
            observed_identity=identity,
        )
        self.assertEqual(record.binding, BINDING_MATCHED)
        self.assertTrue(
            record.admits_actuation,
            "a reinstall that changed nothing this lane runs must not fence it off",
        )

    def test_preflight_mutates_nothing(self) -> None:
        """It describes; it never repairs. No install, no override rewrite, no update."""
        bindir = os.path.join(self.root, "os", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        before = {
            os.path.join(d, f): os.stat(os.path.join(d, f)).st_mtime_ns
            for d, _, files in os.walk(self.root)
            for f in files
        }
        env_before = dict(env)
        evaluate_update_authority(
            "fakex", env, registry=self.registry, updater_targets=self._probe(bindir)
        )
        after = {
            os.path.join(d, f): os.stat(os.path.join(d, f)).st_mtime_ns
            for d, _, files in os.walk(self.root)
            for f in files
        }
        self.assertEqual(before, after)
        self.assertEqual(env, env_before)


class SplitLaneIsNeverHealthyResidencyTest(unittest.TestCase):
    """Acceptance 3 — an update-caused clean exit / self-heal is not a success."""

    def _classify(self, **overrides) -> str:
        facts = {
            "inventory_readable": True,
            "row_present": True,
            "row_stale": False,
            "live_locator": "w4B:p51",
            "launched_locator": "w4B:p51",
            "screen": SCREEN_CLEAR,
            "attestation": ATTESTATION_OK,
        }
        facts.update(overrides)
        return classify_startup_health(**facts)

    def test_baseline_is_healthy(self) -> None:
        self.assertEqual(self._classify(), HEALTH_HEALTHY)

    def test_split_authority_downgrades_a_would_be_green(self) -> None:
        self.assertEqual(
            self._classify(update_authority=AUTHORITY_SPLIT),
            HEALTH_UPDATE_AUTHORITY_SPLIT,
        )

    def test_drifted_binding_downgrades_a_would_be_green(self) -> None:
        self.assertEqual(
            self._classify(executable_binding=BINDING_DRIFTED),
            HEALTH_EXECUTABLE_BINDING_DRIFT,
        )

    def test_undecidable_authority_downgrades_a_would_be_green(self) -> None:
        for facts in (
            {"update_authority": AUTHORITY_UNKNOWN},
            {"executable_binding": BINDING_UNKNOWN},
        ):
            with self.subTest(facts=facts):
                self.assertEqual(
                    self._classify(**facts), HEALTH_UPDATE_AUTHORITY_UNVERIFIED
                )

    def test_same_version_reinstall_stays_healthy(self) -> None:
        """A reinstall that changed nothing must not flap a healthy lane."""
        self.assertEqual(
            self._classify(
                update_authority=AUTHORITY_ALIGNED, executable_binding=BINDING_MATCHED
            ),
            HEALTH_HEALTHY,
        )

    def test_update_caused_clean_exit_is_not_success(self) -> None:
        """The live shape: the gateway exited 0 seconds after reporting ready."""
        self.assertEqual(
            self._classify(row_present=False, update_authority=AUTHORITY_SPLIT),
            HEALTH_PROVIDER_EXITED,
        )

    def test_update_screen_still_outranks_the_authority_gate(self) -> None:
        """An observed live cause is more actionable than a configuration one."""
        self.assertEqual(
            self._classify(screen=SCREEN_BLOCKED, update_authority=AUTHORITY_SPLIT),
            HEALTH_STARTUP_INTERACTION,
        )

    def test_unreadable_inventory_is_never_masked_by_the_authority_gate(self) -> None:
        self.assertEqual(
            self._classify(inventory_readable=False, update_authority=AUTHORITY_SPLIT),
            HEALTH_INVENTORY_UNREADABLE,
        )

    def test_split_outranks_the_evidence_reporting_gap(self) -> None:
        """A diagnosis outranks a gap in reporting."""
        self.assertEqual(
            self._classify(
                update_authority=AUTHORITY_SPLIT, evidence=EVIDENCE_UNAVAILABLE
            ),
            HEALTH_UPDATE_AUTHORITY_SPLIT,
        )

    def test_evidence_gate_is_unchanged_when_authority_is_not_evaluated(self) -> None:
        """Byte-invariance: a caller that does not arm #14741 sees pre-#14741 verdicts."""
        self.assertEqual(
            self._classify(
                evidence=EVIDENCE_UNAVAILABLE,
                update_authority=AUTHORITY_NOT_EVALUATED,
                executable_binding=BINDING_NOT_EVALUATED,
            ),
            HEALTH_STARTUP_EVIDENCE_UNAVAILABLE,
        )

    def test_every_health_token_has_a_fixed_operator_sentence(self) -> None:
        self.assertEqual(set(HEALTH_DETAIL), HEALTH_OUTCOMES)


class BuiltinUpdaterTargetAdapterTest(unittest.TestCase):
    """Design Answer j#96167 items 3-5: positive resolution, or a typed reason. Never a guess.

    The manager is never actually invoked: the runner is a fake with `subprocess.run`'s
    shape, so nothing here depends on an installed npm or on which package manager owns
    the host.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.bindir = os.path.join(self.root, "bin")
        os.makedirs(self.bindir, exist_ok=True)
        npm = os.path.join(self.bindir, "npm")
        with open(npm, "w", encoding="utf-8") as h:
            h.write("#!/bin/sh\nexit 0\n")
        os.chmod(npm, os.stat(npm).st_mode | stat.S_IXUSR)
        self.env = {"PATH": self.bindir}

    def _runner(self, stdout="", returncode=0, raises=None):
        def run(argv, **kwargs):
            if raises is not None:
                raise raises
            self.assertEqual(argv[1:], ["root", "-g"], "only the allowlisted query runs")
            return types.SimpleNamespace(returncode=returncode, stdout=stdout)

        return run

    def test_positive_resolution_requires_the_package_directory_to_exist(self) -> None:
        node_modules = os.path.join(self.root, "lib", "node_modules")
        pkg = os.path.join(node_modules, "@openai", "codex")
        os.makedirs(pkg, exist_ok=True)
        res = resolve_updater_target(
            "codex", self.env, runner=self._runner(stdout=node_modules + "\n")
        )
        self.assertTrue(res.resolved)
        self.assertEqual(res.reason, REASON_OK)
        self.assertEqual(res.roots, (os.path.realpath(pkg),))

    def test_query_answer_without_this_provider_package_is_uncorresponded(self) -> None:
        """The manager writes somewhere, but not for THIS provider — never `aligned`."""
        node_modules = os.path.join(self.root, "lib", "node_modules")
        os.makedirs(node_modules, exist_ok=True)
        res = resolve_updater_target(
            "codex", self.env, runner=self._runner(stdout=node_modules)
        )
        self.assertFalse(res.resolved)
        self.assertEqual(res.reason, REASON_IDENTITY_UNCORRESPONDED)

    def test_unregistered_provider_is_typed_not_guessed(self) -> None:
        res = resolve_updater_target("claude", self.env, runner=self._runner())
        self.assertFalse(res.resolved)
        self.assertEqual(res.reason, REASON_PROVIDER_UNREGISTERED)

    def test_missing_or_unsafe_path_manager_is_typed(self) -> None:
        """No manager, or a PATH we refuse to read, is still unresolvable.

        A *shadowed* second npm is deliberately NOT in this list any more: Design Answer
        D2 (j#96288 item 4) ruled that the effective — first trusted-PATH — executable is
        the one an updater would run, and treating its shadow as undecidable took a
        workspace offline (j#96202). `D2EffectiveManagerResolutionTest` pins that.
        """
        for env in (
            {"PATH": os.path.join(self.root, "empty")},
            {"PATH": os.pathsep.join([self.bindir, "relative/dir"])},
            {},
        ):
            with self.subTest(env=env):
                res = resolve_updater_target("codex", env, runner=self._runner())
                self.assertFalse(res.resolved)
                self.assertEqual(res.reason, REASON_QUERY_EXECUTABLE_UNRESOLVED)

    def test_failed_relative_or_raising_query_is_typed(self) -> None:
        for runner in (
            self._runner(returncode=1, stdout="/x"),
            self._runner(stdout=""),
            self._runner(stdout="relative/dir"),
            self._runner(raises=OSError("boom")),
        ):
            with self.subTest(runner=runner):
                res = resolve_updater_target("codex", self.env, runner=runner)
                self.assertFalse(res.resolved)
                self.assertEqual(res.reason, REASON_QUERY_FAILED)

    def test_resolution_never_leaks_a_path_into_the_reason(self) -> None:
        res = resolve_updater_target("codex", self.env, runner=self._runner(stdout="/nope"))
        self.assertNotIn("/", res.reason)


class ProductionOrchestrationFenceTest(unittest.TestCase):
    """j#96060 F3 / Answer j#96167 item 6 — drive the PRODUCTION caller, not the gate.

    R2's tests called `admit_receiver_startup_or_die` directly and injected the probe
    themselves, so they stayed green while `commands.py` passed nothing. These assert on
    the caller's own wiring: the probe reaching the fence is the thing under test.
    """

    def test_command_module_keeps_its_single_unchanged_call(self) -> None:
        """The composition lives in the gate, so the largest module gains nothing."""
        import inspect

        from mozyo_bridge.application import commands

        source = inspect.getsource(commands)
        self.assertNotIn("builtin_updater_target_probe", source)

    def test_launch_preflight_refuses_a_relaunch_it_cannot_vouch_for(self) -> None:
        """The whole-plan launch fence — the path a lane self-heal re-enters."""
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            AgentProviderExecutableError,
            preflight_launch_providers,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = os.path.join(tmp.name, "bin")
        _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": os.path.join(bindir, "fakex")}
        registry = _fake_profile_registry()

        # Unarmed: byte-invariant with pre-#14741 (no probe supplied).
        self.assertIn(
            "fakex", preflight_launch_providers(["fakex"], env, registry=registry)
        )
        # Armed and unresolved: zero-relaunch, and nothing was started.
        with self.assertRaises(AgentProviderExecutableError):
            preflight_launch_providers(
                ["fakex"], env, registry=registry, updater_targets=lambda pid: ((), False)
            )
        # Armed and positively aligned: the launch plan resolves as before.
        self.assertIn(
            "fakex",
            preflight_launch_providers(
                ["fakex"],
                env,
                registry=registry,
                updater_targets=lambda pid: ([bindir], True),
            ),
        )


class D2CompositionScopeTest(unittest.TestCase):
    """Design Answer D2 (j#96288) item 5 — the behavioral pins that must all hold.

    R3 armed the authority fence for every provider from an ambient default inside the
    generic gate. That refused every Claude send on every host and made 29 unrelated tests
    read the live host's npm. These pin the corrected scope directly, and none of them
    consults the host: the resolver is either absent by design or an explicit fake.
    """

    def setUp(self) -> None:
        self.emitted: list = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _admit(self, receiver, pane=None, **kwargs):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_composition import (  # noqa: E501
            admit_receiver_startup_or_die,
        )

        admit_receiver_startup_or_die(
            herdr_send=True,
            receiver=receiver,
            target="w4B:p51",
            read_lines=40,
            capture_pane=lambda t, n: pane if pane is not None else READY_COMPOSER,
            emit=lambda o, **k: self.emitted.append(o),
            record_format="text",
            record_command=None,
            **kwargs,
        )

    def test_ready_claude_without_a_binding_still_sends_and_never_touches_npm(self) -> None:
        """D2 item 1, and the direct regression for the 29 failures."""
        import mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter as adapter

        consulted: list = []
        real = adapter.resolve_updater_target
        adapter.resolve_updater_target = lambda *a, **k: consulted.append(True)
        self.addCleanup(setattr, adapter, "resolve_updater_target", real)

        self._admit("claude")
        self.assertEqual(self.emitted, [], "an unbound provider is out of this gate's scope")
        self.assertEqual(consulted, [], "and the host's package manager is never consulted")

    def test_unbound_provider_is_not_evaluated_never_unknown(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_composition import (  # noqa: E501
            updater_target_resolver_for,
        )

        self.assertIsNone(updater_target_resolver_for("claude"))
        self.assertEqual(
            evaluate_update_authority("claude", {}).authority,
            AUTHORITY_NOT_EVALUATED,
            "'nobody asked' and 'we asked and could not tell' are different facts",
        )

    def test_update_prompt_is_refused_regardless_of_binding(self) -> None:
        """D2 item 2: an observed update screen is zero-send even for an unbound provider.

        The declared-blocker path does this, so the guarantee does not depend on the
        authority gate being armed.
        """
        with self.assertRaises(SystemExit):
            self._admit("codex", pane=CAPTURED_UPDATE_PROMPT)
        self.assertEqual(self.emitted[0].reason, "receiver_startup_interaction_required")

    def test_supported_provider_is_armed_and_split_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self._admit("codex", updater_targets=lambda pid: ([self.tmp.name], True))
        self.assertEqual(self.emitted[0].reason, "receiver_update_authority_split")

    def test_supported_provider_unknown_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self._admit("codex", updater_targets=lambda pid: ((), False))
        self.assertEqual(self.emitted[0].reason, "receiver_update_authority_split")

    def test_generic_gate_constructs_no_probe_of_its_own(self) -> None:
        """D2 item 3: arming is a composition decision, never an ambient gate default."""
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (  # noqa: E501
            startup_admission_gate as gate,
        )

        self.assertNotIn("builtin_updater_target_probe", inspect.getsource(gate))

    def test_command_module_keeps_one_neutral_dependency(self) -> None:
        import inspect

        from mozyo_bridge.application import commands

        source = inspect.getsource(commands)
        self.assertIn("startup_admission_composition", source)
        for leaked in ("builtin_updater_target_probe", "npm", "@openai"):
            self.assertNotIn(leaked, source)


class D2EffectiveManagerResolutionTest(unittest.TestCase):
    """D2 item 4 — a shadowed second npm is not ambiguity; the effective one is asked."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _npm_dir(self, name):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "npm")
        with open(p, "w", encoding="utf-8") as h:
            h.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return d, os.path.realpath(p)

    def test_first_trusted_path_npm_is_the_one_asked(self) -> None:
        first_dir, first = self._npm_dir("first")
        second_dir, second = self._npm_dir("second")
        node_modules = os.path.join(self.root, "nm")
        os.makedirs(os.path.join(node_modules, "@openai", "codex"), exist_ok=True)
        asked: list = []

        def runner(argv, **kwargs):
            asked.append(argv[0])
            return types.SimpleNamespace(returncode=0, stdout=node_modules)

        res = resolve_updater_target(
            "codex",
            {"PATH": os.pathsep.join([first_dir, second_dir])},
            runner=runner,
        )
        self.assertTrue(res.resolved, "a shadowed npm must not make this undecidable")
        self.assertEqual(asked, [first], "the EFFECTIVE npm is the one interrogated")
        self.assertNotIn(second, asked)

    def test_unsafe_or_relative_path_is_still_unknown(self) -> None:
        first_dir, _ = self._npm_dir("first")
        for path in (os.pathsep.join([first_dir, "relative/dir"]), ""):
            with self.subTest(path=path):
                res = resolve_updater_target(
                    "codex", {"PATH": path}, runner=lambda *a, **k: None
                )
                self.assertFalse(res.resolved)
                self.assertEqual(res.reason, REASON_QUERY_EXECUTABLE_UNRESOLVED)

    def test_managed_provider_executable_is_not_relaxed_to_first_match(self) -> None:
        """The first-match rule is for the MANAGER only; the provider keeps exact identity."""
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            AgentProviderExecutableError,
            resolve_agent_launch,
        )

        a = os.path.join(self.root, "a")
        b = os.path.join(self.root, "b")
        _make_executable(a)
        _make_executable(b)
        with self.assertRaises(AgentProviderExecutableError):
            resolve_agent_launch(
                "fakex",
                {"PATH": os.pathsep.join([a, b])},
                registry=_fake_profile_registry(),
            )


class ActualOrchestratorFenceTest(unittest.TestCase):
    """Review j#96360 F2 — drive the REAL `orchestrate_handoff`, not a helper.

    Three rounds of this issue were reported as wired while the production caller supplied
    nothing, because every test measured a helper boundary (`admit_receiver_startup_or_die`,
    `preflight_launch_providers`) that the tests themselves armed. These call the actual
    orchestrator, so an unwired composition root shows up as a passing send instead of a
    refusal — the failure mode that kept slipping through.
    """

    def _orchestrate(self, *, receiver, pane, resolution):
        """Substitute the HOST boundary only — never the composition's own decision.

        The first cut of this helper patched `updater_target_resolver_for`, which is the
        decision under test, so unwiring the composition root left every assertion green:
        the same mistake j#96360 F2 named. Patching `resolve_updater_target` instead keeps
        the test hermetic while leaving "is the fence armed for this receiver?" to
        production code.
        """
        import mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_composition as comp
        import mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter as adapter

        real = adapter.resolve_updater_target
        adapter.resolve_updater_target = lambda *a, **k: resolution
        self.addCleanup(setattr, adapter, "resolve_updater_target", real)

        emitted: list = []
        try:
            comp.admit_receiver_startup_or_die(
                herdr_send=True,
                receiver=receiver,
                target="w4B:p51",
                read_lines=40,
                capture_pane=lambda t, n: pane,
                emit=lambda o, **k: emitted.append(o),
                record_format="text",
                record_command=None,
            )
            return "SENT", emitted
        except SystemExit:
            return "REFUSED", emitted

    def test_real_send_path_refuses_a_split_codex_lane(self) -> None:
        unresolved = UpdaterTargetResolution(roots=(), resolved=False, reason=REASON_QUERY_FAILED)
        verdict, emitted = self._orchestrate(
            receiver="codex", pane=READY_COMPOSER, resolution=unresolved
        )
        self.assertEqual(verdict, "REFUSED")
        self.assertEqual(emitted[0].reason, "receiver_update_authority_split")

    def test_real_send_path_admits_an_unbound_provider(self) -> None:
        unresolved = UpdaterTargetResolution(roots=(), resolved=False, reason=REASON_QUERY_FAILED)
        verdict, emitted = self._orchestrate(
            receiver="claude", pane=READY_COMPOSER, resolution=unresolved
        )
        self.assertEqual(verdict, "SENT")
        self.assertEqual(emitted, [])


class ActualLaunchAndSelfHealFenceTest(unittest.TestCase):
    """Review j#96360 F1/F2 — the production launch path, and the self-heal that re-enters it.

    `herdr_session_start` is the one production caller of `preflight_launch_providers`, and
    a lane self-heal re-launches through it. R4 added the parameter and left this caller
    passing nothing; this pins the caller itself rather than the helper.
    """

    def test_production_launch_path_arms_the_fence(self) -> None:
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )

        # The arming lives in the composed preflight the launch path imports, not in a
        # kwarg at the call site: `herdr_session_start` sits just under the module-health
        # threshold and a self-approved allowlist entry is not an option, so the wiring is
        # an import redirect exactly as it is for `commands.py`.
        source = inspect.getsource(herdr_session_start)
        self.assertIn("agent_provider_launch_composition import", source)
        self.assertNotIn("agent_provider_executable import", source)

        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application import (  # noqa: E501
            agent_provider_launch_composition as comp,
        )

        self.assertIs(
            herdr_session_start.preflight_launch_providers,
            comp.preflight_launch_providers,
            "the production launch path must resolve to the ARMED preflight",
        )

    def test_composed_launch_preflight_arms_without_being_asked(self) -> None:
        """Behavioral, not structural: the composed preflight must fence a codex launch
        even though the caller passes no `updater_targets`.

        The import-identity assertion above proves the right symbol is wired; it cannot
        prove the symbol still arms. This one goes RED the moment the composition stops
        supplying the resolver — which is the property j#96360 F1/F2 asked for.
        """
        import mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter as adapter
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
            preflight_launch_providers,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            AgentProviderExecutableError,
        )

        real = adapter.resolve_updater_target
        adapter.resolve_updater_target = lambda *a, **k: UpdaterTargetResolution(
            roots=(), resolved=False, reason=REASON_QUERY_FAILED
        )
        self.addCleanup(setattr, adapter, "resolve_updater_target", real)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = os.path.join(tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        exe = os.path.join(bindir, "codex")
        with open(exe, "w", encoding="utf-8") as h:
            h.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)

        with self.assertRaises(AgentProviderExecutableError):
            preflight_launch_providers(
                ["codex"], {"PATH": bindir, "MOZYO_AGENT_CODEX_BINARY": exe}
            )

    def test_launch_composition_scopes_per_provider(self) -> None:
        """A bound sibling must not drag an unbound provider into the fence (D2 item 1)."""
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
            launch_updater_target_resolver,
        )

        self.assertIsNone(launch_updater_target_resolver(["claude"]))
        mixed = launch_updater_target_resolver(["codex", "claude"])
        self.assertIsNotNone(mixed)
        self.assertIsNone(mixed("claude"), "unbound stays not_evaluated inside a mixed plan")

    def test_relaunch_is_refused_when_the_binding_drifted(self) -> None:
        """An update rewrote the executable under the lane: the self-heal must not restart it."""
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
            AgentProviderExecutableError,
            preflight_launch_providers,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = os.path.join(tmp.name, "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        registry = _fake_profile_registry()

        with self.assertRaises(AgentProviderExecutableError):
            preflight_launch_providers(
                ["fakex"],
                env,
                registry=registry,
                updater_targets=lambda pid: ([bindir], True),
                bound_identities={"fakex": executable_identity(pinned, "0.145.0")},
                observed_versions={"fakex": "0.146.0"},
            )

        # Same-version reinstall changed nothing this lane runs -> the relaunch proceeds.
        self.assertIn(
            "fakex",
            preflight_launch_providers(
                ["fakex"],
                env,
                registry=registry,
                updater_targets=lambda pid: ([bindir], True),
                bound_identities={"fakex": executable_identity(pinned, "0.146.0")},
                observed_versions={"fakex": "0.146.0"},
            ),
        )


class QueryEnvironmentTest(unittest.TestCase):
    """Review j#96360 F3 — the query runs under the env its executable was resolved from."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bindir, exist_ok=True)
        npm = os.path.join(self.bindir, "npm")
        with open(npm, "w", encoding="utf-8") as h:
            h.write("#!/bin/sh\nexit 0\n")
        os.chmod(npm, os.stat(npm).st_mode | stat.S_IXUSR)

    def test_runner_receives_the_trusted_env_not_the_ambient_one(self) -> None:
        seen: dict = {}

        def runner(argv, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(returncode=0, stdout="/nowhere")

        env = {"PATH": self.bindir, "NPM_CONFIG_PREFIX": "/elsewhere"}
        resolve_updater_target("codex", env, runner=runner)
        self.assertEqual(
            seen.get("env"),
            env,
            "a stray NPM_CONFIG_PREFIX in the ambient env would answer about a different "
            "global root than the one being evaluated (j#96360 F3)",
        )

    def test_no_retired_placeholder_tests_remain(self) -> None:
        """j#96360 F2 also flagged disabled, non-executing tests. None may survive.

        The marker is assembled at runtime so this assertion does not match its own
        source — the same trap as quoting a gate marker literally in a gate journal.
        """
        import inspect

        import tests.regressions.test_issue_14741_codex_update_binary_authority_split as mod

        marker = "_" + "RETIRED" + "_"
        self.assertNotIn(marker, inspect.getsource(mod))


if __name__ == "__main__":
    unittest.main()
