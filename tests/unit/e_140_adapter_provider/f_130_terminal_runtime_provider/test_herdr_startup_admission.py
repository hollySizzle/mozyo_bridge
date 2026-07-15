"""The pure startup-admission classifier + the action-time gate (Redmine #13760).

Splits into the two things that must not be confused:

- the **classifier** (``StartupBlocker`` / ``AgentProviderProfile.match_startup_blocker``,
  a pure function of the profile data and the rendered pane): does the AND actually hold,
  does the rendering survive framing/wrapping, and — the guard that matters most — does a
  single signature quoted somewhere innocent NOT block a ready composer;
- the **gate** (:func:`evaluate_startup_admission`): does it fail closed, in the right
  vocabulary, for the three refusal shapes (a matched screen, an unreadable pane, an
  unprofiled provider), and never leak the pane's text into the record.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (
    ADMISSION_ADMITTED,
    ADMISSION_BLOCKED,
    ADMISSION_UNKNOWN_PROVIDER,
    ADMISSION_UNREADABLE,
    StartupAdmission,
    StartupAdmissionError,
    evaluate_startup_admission,
    startup_admission_record_lines,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    AGENT_PROVIDER_PROFILES,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentProviderProfileConfig,
    AgentProviderProfileError,
    StartupBlocker,
    fold_startup_text,
)

# The trust confirmation as a real TUI paints it: framed, and hard-wrapped mid-token at
# the pane width (the wrap that broke naive substring matching in #13322).
TRUST_SCREEN = (
    "╭──────────────────────────────────────────────╮\n"
    "│ Accessing workspace:                         │\n"
    "│ /workspace/project-alpha/lane_worktree       │\n"
    "│ Quick safety check: Is this a project you    │\n"
    "│ created or one you trust? (Like your own c   │\n"
    "│ ode, a well-known open source project)       │\n"
    "│ Claude Code'll be able to read, edit, and    │\n"
    "│ execute files here.                          │\n"
    "│ ❯ 1. Yes, proceed                            │\n"
    "╰──────────────────────────────────────────────╯"
)

IDLE_COMPOSER = '╭────────────────╮\n│ > Try "fix it" │\n╰────────────────╯\n  ? for shortcuts'


def _profile(provider_id="testprovider", blockers=()):
    record = {
        # v2: the startup_blockers field is the v2 addition, so a profile that declares it
        # must be on a schema version that has it (#13760 review j#78529 F2).
        "version": "2",
        "source": "test",
        "profiles": {
            provider_id: {
                "protocol": "interactive_cli_tui",
                "executable": {"command": "tp", "env_override": "TP_BIN"},
                "capabilities": ["interactive_tui"],
                "startup_blockers": list(blockers),
            }
        },
    }
    return AgentProviderProfileConfig.from_record(record).to_registry()


class FoldStartupTextTest(unittest.TestCase):
    def test_folds_away_framing_wrapping_and_punctuation(self) -> None:
        # A framed, mid-token-wrapped line must fold to the same key as the flat one.
        self.assertEqual(
            fold_startup_text("│ Is this a project you cr   │\n│ eated or one you trust? │"),
            fold_startup_text("Is this a project you created or one you trust"),
        )

    def test_non_string_and_empty_fold_to_empty(self) -> None:
        for value in (None, 123, [], "", "   \n\t", "─│╭╯"):
            self.assertEqual(fold_startup_text(value), "")


class StartupBlockerClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.blocker = StartupBlocker(
            blocker_id="trust",
            all_of=(
                "Is this a project you created or one you trust",
                "be able to read, edit, and execute files here",
            ),
        )

    def test_matches_a_framed_wrapped_screen(self) -> None:
        self.assertTrue(self.blocker.matches(TRUST_SCREEN))

    def test_one_signature_alone_never_matches(self) -> None:
        # THE false-positive guard (j#77947 correction 1). A ready composer whose
        # transcript happens to quote one phrase is not a trust screen — a blocker that
        # fired here would refuse every real dispatch on that lane.
        quoting = (
            f"{IDLE_COMPOSER}\n  earlier: Is this a project you created or one you trust?"
        )
        self.assertFalse(self.blocker.matches(quoting))
        self.assertFalse(
            self.blocker.matches("Claude Code'll be able to read, edit, and execute files here.")
        )

    def test_unreadable_or_empty_content_matches_nothing(self) -> None:
        # Pure and total: the classifier reports "no match", and it is the CALLER's job
        # not to read that as "startup clear" (which is why the gate has a separate
        # unreadable outcome rather than falling through to admitted).
        for content in (None, "", "   ", 42, b"bytes"):
            self.assertFalse(self.blocker.matches(content))

    def test_first_declared_match_wins_deterministically(self) -> None:
        registry = _profile(
            blockers=[
                {"id": "first", "all_of": ["alpha screen", "beta option"]},
                {"id": "second", "all_of": ["alpha screen", "beta option"]},
            ]
        )
        profile = registry.require("testprovider")
        matched = profile.match_startup_blocker("alpha screen / beta option")
        self.assertEqual(matched.blocker_id, "first")


class StartupBlockerTypeInvariantTest(unittest.TestCase):
    """Review j#78481 finding 1: the typed object enforces the FULL invariant itself.

    Before the fix, element validation lived only in `from_record`, so a directly-built
    `StartupBlocker` could hold a malformed `all_of` — and because `fold_startup_text(None)`
    / `fold_startup_text("")` is `""` and `"" in folded` is always true, the AND silently
    degraded to a single-signature match (the exact false positive the AND exists to stop).
    """

    def test_direct_construction_with_none_element_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            StartupBlocker("trust", ("one generic phrase", None))

    def test_direct_construction_with_blank_element_is_rejected(self) -> None:
        for blank in ("", "   ", "\t\n"):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(AgentProviderProfileError):
                    StartupBlocker("trust", ("one generic phrase", blank))

    def test_and_no_longer_degrades_to_a_single_signature(self) -> None:
        # The regression itself: a malformed 2-tuple must not match a composer that only
        # contains the first (real) signature. Since construction now raises, the object
        # that would have false-positived cannot exist.
        with self.assertRaises(AgentProviderProfileError):
            StartupBlocker("trust", ("one generic phrase", None)).matches(
                "ready composer with one generic phrase inside"
            )

    def test_direct_construction_rejects_near_empty_duplicate_and_long(self) -> None:
        cases = [
            ("blocker id", ("> ?", "a real phrase here")),  # near-empty folded
            ("blocker id", ("same phrase here", "same phrase here")),  # duplicate
            ("blocker id", ("a real phrase here", "x" * 200)),  # over length bound
            ("blocker id", ("only one signature",)),  # below AND arity
            ("blocker id", "a bare string is iterated per char"),  # not a tuple
            ("", ("phrase one here", "phrase two here")),  # blank id
            ("coordinator", ("phrase one here", "phrase two here")),  # forbidden token id
        ]
        for blocker_id, all_of in cases:
            with self.subTest(all_of=all_of):
                with self.assertRaises(AgentProviderProfileError):
                    StartupBlocker(blocker_id, all_of)

    def test_valid_direct_construction_still_works_and_and_holds(self) -> None:
        blocker = StartupBlocker("trust", ("phrase one here", "phrase two here"))
        self.assertTrue(blocker.matches("phrase one here and phrase two here"))
        self.assertFalse(blocker.matches("phrase one here only"))


class StartupBlockerFoldedIndependenceTest(unittest.TestCase):
    """Review j#78529 finding 1: independence is checked in the FOLDED domain.

    The R1 duplicate guard compared RAW signatures, but the matcher folds every signature
    before the substring test. So two raw-distinct signatures that fold to the same key
    (case / punctuation / whitespace variants), or one whose folded key is a substring of
    another's, would pass the R1 guard yet let a SINGLE displayed phrase satisfy every AND
    term — the exact collapse the AND exists to prevent.
    """

    def test_case_only_variant_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            StartupBlocker("trust", ("Generic Phrase", "generic phrase"))

    def test_punctuation_only_variant_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            StartupBlocker("trust", ("generic phrase", "generic-phrase"))

    def test_whitespace_only_variant_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            StartupBlocker("trust", ("generic phrase   ", "generic phrase"))

    def test_substring_containment_is_rejected(self) -> None:
        # A pane showing "generic phrase here" would match BOTH, so the two are not
        # independent AND terms — the AND reduces to the longer one alone.
        with self.assertRaises(AgentProviderProfileError):
            StartupBlocker("trust", ("generic phrase", "generic phrase here"))

    def test_a_single_phrase_can_no_longer_satisfy_the_whole_and(self) -> None:
        # The regression property: for every fold-collapsing pair, the object that would
        # have let one phrase match all AND terms cannot be constructed.
        for pair in [
            ("generic phrase", "generic-phrase"),
            ("Generic Phrase", "generic phrase"),
            ("generic phrase", "generic phrase here"),
        ]:
            with self.subTest(pair=pair):
                with self.assertRaises(AgentProviderProfileError):
                    StartupBlocker("trust", pair)

    def test_record_load_also_rejects_fold_collapsing_signatures(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            _profile(blockers=[{"id": "trust", "all_of": ["generic phrase", "generic-phrase"]}])

    def test_genuinely_independent_signatures_are_accepted(self) -> None:
        blocker = StartupBlocker("trust", ("phrase one alpha", "phrase two beta"))
        self.assertFalse(blocker.matches("phrase one alpha only"))
        self.assertTrue(blocker.matches("phrase one alpha and phrase two beta"))

    def test_every_packaged_blocker_has_independent_signatures(self) -> None:
        # The shipped Claude blockers must satisfy the stronger independence rule (they are
        # re-validated on every load, so this pins that the packaged data actually passes).
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            AGENT_PROVIDER_PROFILES,
        )

        for blocker in AGENT_PROVIDER_PROFILES.require("claude").startup_blockers:
            folded = [fold_startup_text(sig) for sig in blocker.all_of]
            for i, a in enumerate(folded):
                for j, b in enumerate(folded):
                    if i != j:
                        self.assertFalse(
                            a in b,
                            msg=f"{blocker.blocker_id}: folded {a!r} ⊆ {b!r}",
                        )


class ConfigVersionShapeGateTest(unittest.TestCase):
    """Review j#78529 finding 2: the declared version gates the accepted shape.

    A `version: "1"` artifact (the pre-startup_blockers shape) may not carry
    `startup_blockers`; `version: "2"` may. Without this, the "v2 adds startup_blockers"
    contract drifts from what the loader actually honors.
    """

    def _config(self, version, *, with_blockers):
        profile = {
            "protocol": "interactive_cli_tui",
            "executable": {"command": "p", "env_override": "P_BIN"},
        }
        if with_blockers:
            profile["startup_blockers"] = [
                {"id": "trust", "all_of": ["phrase one alpha", "phrase two beta"]}
            ]
        return AgentProviderProfileConfig.from_record(
            {"version": version, "source": "test", "profiles": {"p": profile}}
        )

    def test_v1_without_startup_blockers_loads(self) -> None:
        config = self._config("1", with_blockers=False)
        self.assertEqual(config.profiles[0].startup_blockers, ())

    def test_v1_with_startup_blockers_fails_closed(self) -> None:
        with self.assertRaises(AgentProviderProfileError) as ctx:
            self._config("1", with_blockers=True)
        self.assertIn("startup_blockers", str(ctx.exception))

    def test_v2_with_and_without_startup_blockers_both_load(self) -> None:
        self.assertEqual(len(self._config("2", with_blockers=True).profiles[0].startup_blockers), 1)
        self.assertEqual(self._config("2", with_blockers=False).profiles[0].startup_blockers, ())


class ConfigSchemaVersionTest(unittest.TestCase):
    """Review j#78481 finding 2: an unknown schema version fails closed at load."""

    def _config(self, version):
        record = {
            "version": version,
            "source": "test",
            "profiles": {
                "p": {
                    "protocol": "interactive_cli_tui",
                    "executable": {"command": "p", "env_override": "P_BIN"},
                }
            },
        }
        return AgentProviderProfileConfig.from_record(record)

    def test_supported_versions_load(self) -> None:
        for version in ("1", "2"):
            with self.subTest(version=version):
                self.assertEqual(self._config(version).version, version)

    def test_unknown_version_fails_closed(self) -> None:
        with self.assertRaises(AgentProviderProfileError) as ctx:
            self._config("999-unknown")
        self.assertIn("unsupported schema version", str(ctx.exception))

    def test_packaged_artifact_declares_a_supported_version(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
            SUPPORTED_SCHEMA_VERSIONS,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            load_agent_provider_config,
        )

        config = load_agent_provider_config()
        self.assertIn(config.version, SUPPORTED_SCHEMA_VERSIONS)
        # The packaged artifact actually USES the v2 startup_blockers, so it must declare v2.
        self.assertEqual(config.version, "2")


class StartupBlockerSchemaTest(unittest.TestCase):
    def test_single_signature_blocker_is_rejected(self) -> None:
        # The AND arity is enforced at LOAD, not left to reviewer discipline: a
        # one-phrase blocker is exactly the false-positive shape j#77947 forbids.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            _profile(blockers=[{"id": "trust", "all_of": ["Yes, proceed"]}])
        self.assertIn("all_of", str(ctx.exception))

    def test_unknown_blocker_key_fails_closed(self) -> None:
        # A profile may never grow a field that ANSWERS the screen. An unknown key is
        # rejected rather than ignored, so `keys:` / `accept:` can never be smuggled in
        # and honored by a future reader.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            _profile(
                blockers=[
                    {
                        "id": "trust",
                        "all_of": ["one phrase here", "another phrase here"],
                        "accept_keys": "enter",
                    }
                ]
            )
        self.assertIn("accept_keys", str(ctx.exception))

    def test_missing_key_and_bad_types_fail_closed(self) -> None:
        for bad in (
            [{"id": "trust"}],  # no all_of
            [{"all_of": ["one phrase here", "another phrase"]}],  # no id
            [{"id": "", "all_of": ["one phrase here", "another phrase"]}],
            [{"id": "trust", "all_of": "a bare string is iterated per character"}],
            [{"id": "trust", "all_of": ["ok phrase here", 7]}],
            ["not a mapping"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(AgentProviderProfileError):
                    _profile(blockers=bad)

    def test_near_empty_signature_is_rejected(self) -> None:
        # A signature that folds to almost nothing would match every screen — the
        # fail-OPEN-in-reverse mistake (it would block everything).
        with self.assertRaises(AgentProviderProfileError) as ctx:
            _profile(blockers=[{"id": "trust", "all_of": ["> ?", "a real phrase here"]}])
        self.assertIn("alphanumeric", str(ctx.exception))

    def test_duplicate_blocker_ids_are_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            _profile(
                blockers=[
                    {"id": "trust", "all_of": ["phrase one here", "phrase two here"]},
                    {"id": "trust", "all_of": ["phrase three here", "phrase four here"]},
                ]
            )

    def test_blocker_may_not_claim_an_authority_token(self) -> None:
        # A profile describes a screen; it can never name a workflow role / binding.
        with self.assertRaises(AgentProviderProfileError):
            _profile(
                blockers=[
                    {"id": "coordinator", "all_of": ["phrase one here", "phrase two here"]}
                ]
            )

    def test_absent_startup_blockers_is_valid_and_admits(self) -> None:
        profile = _profile().require("testprovider")
        self.assertEqual(profile.startup_blockers, ())
        self.assertIsNone(profile.match_startup_blocker(TRUST_SCREEN))


class PackagedClaudeProfileTest(unittest.TestCase):
    """The shipped data must actually classify the screens #13760 was raised on."""

    def test_claude_declares_the_observed_startup_screens(self) -> None:
        profile = AGENT_PROVIDER_PROFILES.require("claude")
        ids = {blocker.blocker_id for blocker in profile.startup_blockers}
        self.assertEqual(
            ids,
            {
                "workspace_trust_confirmation",
                "directory_trust_confirmation",
                "first_run_theme",
                "login_required",
            },
        )

    def test_packaged_claude_profile_matches_the_live_trust_screen(self) -> None:
        profile = AGENT_PROVIDER_PROFILES.require("claude")
        matched = profile.match_startup_blocker(TRUST_SCREEN)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.blocker_id, "workspace_trust_confirmation")

    def test_packaged_claude_profile_admits_a_ready_composer(self) -> None:
        profile = AGENT_PROVIDER_PROFILES.require("claude")
        self.assertIsNone(profile.match_startup_blocker(IDLE_COMPOSER))


class EvaluateStartupAdmissionTest(unittest.TestCase):
    def test_matched_screen_blocks_and_names_the_blocker(self) -> None:
        admission = evaluate_startup_admission(
            provider_id="claude", read_visible=lambda: TRUST_SCREEN
        )
        self.assertFalse(admission.admitted)
        self.assertEqual(admission.outcome, ADMISSION_BLOCKED)
        self.assertEqual(admission.blocker_id, "workspace_trust_confirmation")

    def test_ready_composer_is_admitted(self) -> None:
        admission = evaluate_startup_admission(
            provider_id="claude", read_visible=lambda: IDLE_COMPOSER
        )
        self.assertTrue(admission.admitted)
        self.assertEqual(admission.outcome, ADMISSION_ADMITTED)
        self.assertEqual(admission.blocker_id, "")

    def test_read_failure_is_unreadable_not_admitted(self) -> None:
        def boom():
            raise RuntimeError("herdr read_pane failed")

        admission = evaluate_startup_admission(provider_id="claude", read_visible=boom)
        self.assertEqual(admission.outcome, ADMISSION_UNREADABLE)
        self.assertFalse(admission.admitted)

    def test_die_shaped_systemexit_is_unreadable_not_a_crash(self) -> None:
        # The tmux-era primitives fail closed through `die()` == SystemExit, which an
        # `except Exception` would not catch (review j#71597's precedent). A read that
        # exits must still become a structured zero-send refusal, never a process exit
        # that escapes the outcome path.
        def die():
            raise SystemExit(1)

        admission = evaluate_startup_admission(provider_id="claude", read_visible=die)
        self.assertEqual(admission.outcome, ADMISSION_UNREADABLE)

    def test_blank_read_is_unreadable_not_startup_clear(self) -> None:
        # A live TUI always paints something; a blank read is evidence of nothing, and
        # "we saw nothing, so it must be fine" is precisely the decay j#77947 invariant 4
        # forbids.
        for blank in ("", "   \n  ", None):
            with self.subTest(blank=repr(blank)):
                admission = evaluate_startup_admission(
                    provider_id="claude", read_visible=lambda b=blank: b
                )
                self.assertEqual(admission.outcome, ADMISSION_UNREADABLE)

    def test_unknown_provider_fails_closed_without_reading(self) -> None:
        reads = []

        def read():
            reads.append(1)
            return IDLE_COMPOSER

        admission = evaluate_startup_admission(provider_id="gemini", read_visible=read)
        self.assertEqual(admission.outcome, ADMISSION_UNKNOWN_PROVIDER)
        self.assertFalse(admission.admitted)
        self.assertEqual(reads, [], "an unprofiled provider is refused before any I/O")

    def test_reads_the_pane_exactly_once(self) -> None:
        reads = []

        def read():
            reads.append(1)
            return IDLE_COMPOSER

        evaluate_startup_admission(provider_id="claude", read_visible=read)
        self.assertEqual(len(reads), 1)


class StartupAdmissionRecordTest(unittest.TestCase):
    def test_blocked_record_carries_tokens_only_never_pane_text(self) -> None:
        admission = evaluate_startup_admission(
            provider_id="claude", read_visible=lambda: TRUST_SCREEN
        )
        rendered = "\n".join(startup_admission_record_lines(admission))
        self.assertIn("workspace_trust_confirmation", rendered)
        # The screen shows the absolute workspace path it is asking you to trust, and
        # this record is pasted verbatim into a durable journal (j#77947 invariant 3).
        # (The fixture uses a neutral synthetic workspace path — no home-path-shaped
        # literal in the tracked tree, #13835 — so the canary keys on that path.)
        self.assertNotIn("/workspace/", rendered)
        self.assertNotIn("Quick safety check", rendered)
        self.assertNotIn("Yes, proceed", rendered)

    def test_admitted_record_is_empty(self) -> None:
        admission = evaluate_startup_admission(
            provider_id="claude", read_visible=lambda: IDLE_COMPOSER
        )
        self.assertEqual(startup_admission_record_lines(admission), [])

    def test_telemetry_dict_is_fixed_tokens(self) -> None:
        admission = evaluate_startup_admission(
            provider_id="claude", read_visible=lambda: TRUST_SCREEN
        )
        self.assertEqual(
            admission.to_telemetry_dict(),
            {
                "outcome": ADMISSION_BLOCKED,
                "provider_id": "claude",
                "blocker_id": "workspace_trust_confirmation",
            },
        )

    def test_blocked_without_a_blocker_id_is_rejected(self) -> None:
        # A blocked verdict that cannot name the screen is unauditable.
        with self.assertRaises(StartupAdmissionError):
            StartupAdmission(outcome=ADMISSION_BLOCKED, provider_id="claude")

    def test_admitted_may_not_carry_a_blocker_id(self) -> None:
        with self.assertRaises(StartupAdmissionError):
            StartupAdmission(
                outcome=ADMISSION_ADMITTED, provider_id="claude", blocker_id="trust"
            )

    def test_unknown_outcome_token_is_rejected(self) -> None:
        with self.assertRaises(StartupAdmissionError):
            StartupAdmission(outcome="probably_fine", provider_id="claude")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
