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

    def test_admits_relaunch_only_for_evaluated_or_unevaluated_positives(self) -> None:
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
                self.assertEqual(record.admits_relaunch, admits)

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

    def test_no_probe_is_unknown_never_aligned(self) -> None:
        """The default. Nothing established where the updater writes, so nothing is claimed."""
        bindir = os.path.join(self.root, "os", "bin")
        pinned = _make_executable(bindir)
        env = {"PATH": bindir, "MOZYO_AGENT_FAKEX_BINARY": pinned}
        record = evaluate_update_authority("fakex", env, registry=self.registry)
        self.assertEqual(record.authority, AUTHORITY_UNKNOWN)
        self.assertEqual(record.updater_targets, 0)
        self.assertFalse(record.admits_relaunch, "a re-launch needs a positive authority")
        self.assertFalse(
            record.proven_wrong_binary,
            "unknown is not a demonstrated wrong binary, so it must not refuse a send",
        )

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
        self.assertTrue(record.admits_relaunch)
        self.assertFalse(record.proven_wrong_binary)

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
        self.assertTrue(
            record.proven_wrong_binary,
            "a demonstrated split must refuse the send: updating cannot fix this lane",
        )
        self.assertFalse(record.admits_relaunch)

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
                self.assertFalse(record.proven_wrong_binary)

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
        bindir = os.path.join(self.root, "os", "bin")
        pinned = _make_executable(bindir)
        env = {
            "PATH": os.pathsep.join([bindir, "relative/dir"]),
            "MOZYO_AGENT_FAKEX_BINARY": pinned,
        }
        record = evaluate_update_authority("fakex", env, registry=self.registry)
        self.assertEqual(record.authority, AUTHORITY_UNKNOWN)

    def test_unresolvable_provider_is_unknown_and_does_not_raise(self) -> None:
        record = evaluate_update_authority(
            "fakex", {"PATH": os.path.join(self.root, "empty")}, registry=self.registry
        )
        self.assertEqual(record.authority, AUTHORITY_UNKNOWN)
        self.assertFalse(record.admits_relaunch)

    def test_unknown_provider_is_unknown_and_does_not_raise(self) -> None:
        record = evaluate_update_authority(
            "no_such_provider", {"PATH": self.root}, registry=self.registry
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
        self.assertTrue(record.proven_wrong_binary)
        self.assertFalse(record.admits_relaunch)

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
        self.assertTrue(record.admits_relaunch)
        self.assertFalse(record.proven_wrong_binary)

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


if __name__ == "__main__":
    unittest.main()


class ProductionSendFenceTest(unittest.TestCase):
    """Acceptance 4 through the PRODUCTION gate (Redmine #14741 review j#95741 F3).

    The first cut's 49 tests all passed while nothing was wired, which is the proof that
    they did not measure acceptance 2/3. These run `admit_receiver_startup_or_die` — the
    same shared pre-send fence `orchestrate_handoff` calls — with fake collaborators, and
    assert on what the *lane* does: whether a body could have been sent.

    "Zero-send" is asserted, never assumed: `capture_pane` is the ONLY collaborator that
    may be touched, and the gate returns before any injection, so a refusal is proven by
    the gate raising while the emitted outcome carries the refusal reason.
    """

    def setUp(self) -> None:
        self.emitted: list = []
        self.ledgered: list = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _gate(self, *, pane: str, receiver: str = "codex", **kwargs):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_gate import (  # noqa: E501
            admit_receiver_startup_or_die,
        )

        reads: list = []

        def capture_pane(target, lines):
            reads.append((target, lines))
            return pane

        def emit(outcome, **_kw):
            self.emitted.append(outcome)

        admit_receiver_startup_or_die(
            herdr_send=True,
            receiver=receiver,
            target="w4B:p51",
            read_lines=40,
            capture_pane=capture_pane,
            emit=emit,
            record_format="text",
            record_command=None,
            ledger=self.ledgered.append,
            **kwargs,
        )
        return reads

    def _probe(self, *roots, resolved=True):
        return lambda provider_id: (list(roots), resolved)

    def _pinned_codex(self, subdir="pm"):
        """A real inert file pinned as the managed codex exec target."""
        bindir = os.path.join(self.root, subdir, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, "codex")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return os.path.realpath(path)

    def test_update_prompt_refuses_the_send_through_the_production_gate(self) -> None:
        """The live #14741 pane: the gate must raise before anything is typed."""
        with self.assertRaises(SystemExit):
            self._gate(pane=CAPTURED_UPDATE_PROMPT)
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[0].reason, "receiver_startup_interaction_required")
        self.assertTrue(self.ledgered, "a refusal must be visible to the supervisor")

    def test_ready_composer_passes_the_production_gate(self) -> None:
        """Positive control at the production seam: a ready lane still sends."""
        reads = self._gate(pane=READY_COMPOSER)
        self.assertEqual(self.emitted, [], "an admitted send emits no refusal")
        self.assertEqual(len(reads), 1, "exactly one action-time read, as before")

    def test_split_authority_refuses_the_send_even_on_a_ready_pane(self) -> None:
        """The whole point of #14741: the pane looks ready and the lane is still wrong."""
        pinned = self._pinned_codex()
        other_root = os.path.join(self.root, "nvm", "lib", "node_modules")
        os.makedirs(other_root, exist_ok=True)
        os.environ["MOZYO_AGENT_CODEX_BINARY"] = pinned
        self.addCleanup(os.environ.pop, "MOZYO_AGENT_CODEX_BINARY", None)
        with self.assertRaises(SystemExit):
            self._gate(pane=READY_COMPOSER, updater_targets=self._probe(other_root))
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[0].reason, "receiver_update_authority_split")
        self.assertTrue(self.ledgered)

    def test_binding_drift_refuses_the_send_even_on_a_ready_pane(self) -> None:
        """An update rewrote the executable under the lane: re-bind, never inherit."""
        pinned = self._pinned_codex()
        os.environ["MOZYO_AGENT_CODEX_BINARY"] = pinned
        self.addCleanup(os.environ.pop, "MOZYO_AGENT_CODEX_BINARY", None)
        with self.assertRaises(SystemExit):
            self._gate(
                pane=READY_COMPOSER,
                updater_targets=self._probe(os.path.join(self.root, "pm")),
                bound_executable_identity=executable_identity(pinned, "0.145.0"),
                observed_executable_identity=executable_identity(pinned, "0.146.0"),
            )
        self.assertEqual(self.emitted[0].reason, "receiver_update_authority_split")

    def test_same_version_reinstall_does_not_fence_the_lane(self) -> None:
        """Update SUCCESS that changed nothing this lane runs: the send proceeds."""
        pinned = self._pinned_codex()
        identity = executable_identity(pinned, "0.146.0")
        os.environ["MOZYO_AGENT_CODEX_BINARY"] = pinned
        self.addCleanup(os.environ.pop, "MOZYO_AGENT_CODEX_BINARY", None)
        self._gate(
            pane=READY_COMPOSER,
            updater_targets=self._probe(os.path.join(self.root, "pm")),
            bound_executable_identity=identity,
            observed_executable_identity=identity,
        )
        self.assertEqual(self.emitted, [])

    def test_unknown_authority_does_not_take_the_workspace_offline(self) -> None:
        """The honest common case (no probe) must not become a workspace-wide outage.

        `unknown` still withholds a re-launch and still keeps the lane out of a green
        startup-health verdict; it does not refuse a send to a live, ready pane.
        """
        self._gate(pane=READY_COMPOSER)
        self.assertEqual(self.emitted, [])

    def test_tmux_path_is_byte_invariant(self) -> None:
        """The non-herdr path keeps its unchanged snapshot preflight and no new gate."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_gate import (  # noqa: E501
            admit_receiver_startup_or_die,
        )

        reads: list = []
        admit_receiver_startup_or_die(
            herdr_send=False,
            receiver="codex",
            target="w4B:p51",
            read_lines=40,
            capture_pane=lambda t, n: reads.append((t, n)) or "",
            emit=lambda *a, **k: self.emitted.append(a),
            record_format="text",
            record_command=None,
        )
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.emitted, [])


class SelfHealRelaunchTest(unittest.TestCase):
    """Acceptance 3/4: a self-heal re-launch must re-verify, never inherit a stale pin."""

    def test_relaunch_is_withheld_for_every_non_positive_authority(self) -> None:
        """`unknown` withholds a RE-LAUNCH even though it does not refuse a send.

        Re-launch is the moment the #14741 loop re-armed itself: restarting a binary
        whose authority nobody established is how the loop stayed invisible.
        """
        for authority in (AUTHORITY_SPLIT, AUTHORITY_UNKNOWN):
            with self.subTest(authority=authority):
                record = UpdateAuthority(provider="codex", authority=authority)
                self.assertFalse(record.admits_relaunch)
        for binding in (BINDING_DRIFTED, BINDING_UNKNOWN):
            with self.subTest(binding=binding):
                record = UpdateAuthority(provider="codex", binding=binding)
                self.assertFalse(record.admits_relaunch)

    def test_health_projection_forwards_the_axes_to_the_classifier(self) -> None:
        """The production health seam actually carries the axes (j#95741 F1)."""
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_startup_health,
        )

        signature = inspect.signature(herdr_startup_health.probe_startup_health)
        self.assertIn("update_authority", signature.parameters)
        self.assertIn("executable_binding", signature.parameters)
        source = inspect.getsource(herdr_startup_health._observe_once)
        self.assertIn("update_authority=update_authority", source)
        self.assertIn("executable_binding=executable_binding", source)
