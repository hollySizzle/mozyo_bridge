"""External-parent delegation child-candidate config + resolver tests (#12549).

Pins the typed ``delegation:`` child-candidate schema boundary and the pure
:func:`resolve_child_candidate` resolver introduced for Redmine #12549 (parent
#12499, Feature #12386 ``Delegated Coordinator / Nested Handoff``). The
acceptance contract and prohibitions live in:

- ``vibes/docs/specs/delegation-policy-project-config.md``
  (``## config knob schema`` / ``## fail-closed / fallback matrix`` /
  ``## Public / Private boundary``)
- the #12547 classical acceptance oracle
  (``tests/unit/execution_platform/test_delegated_coordinator_acceptance_oracle.py``): a missing
  candidate fails closed with ``child_candidate_missing`` and an ambiguous one
  with ``child_candidate_ambiguous``.

These tests are hermetic and operator-route-free: no live tmux, no Redmine
read/write, no handoff send, no private pane ids, no host paths. Fixtures use the
public project token ``mozyo_bridge`` and neutral capability tokens only — an
operator resolves a ``mozyo_bridge`` child candidate without injecting any route
/ target / pane / role hint, exactly the #12549 scope. The diagnostic vocabulary
is asserted against the *shipped* #12547 oracle reason strings so the resolver
cannot silently drift from the oracle it must conform to.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
# Make sibling flat test modules importable by bare name (the #12547 oracle is
# imported in OracleVocabularyConformanceTest) regardless of how unittest names
# this module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config import (  # noqa: E402
    CHILD_CANDIDATE_AMBIGUOUS,
    CHILD_CANDIDATE_MISSING,
    CHILD_CANDIDATE_RESOLVED,
    DELEGATION_CONFIG_VERSION,
    STATUS_AMBIGUOUS,
    STATUS_MISSING,
    STATUS_RESOLVED,
    ChildCandidate,
    ChildCandidateResolution,
    DelegationConfig,
    DelegationConfigError,
    resolve_child_candidate,
)

# The public project token the #12549 acceptance scope names explicitly. It is
# the repo's own name (public, not a private Redmine project name), kept as the
# single fixture identifier so no operator-local routing leaks into the test.
CHILD_PROJECT = "mozyo_bridge"


def _config(*candidates: dict) -> DelegationConfig:
    return DelegationConfig.from_record({"child_candidates": list(candidates)})


class DefaultBehaviorPreservingTest(unittest.TestCase):
    """Absence of a ``delegation:`` block exposes no delegated child candidate."""

    def test_default_has_no_candidates(self) -> None:
        self.assertEqual(DelegationConfig.default().child_candidates, ())

    def test_none_record_is_default(self) -> None:
        self.assertEqual(DelegationConfig.from_record(None), DelegationConfig.default())

    def test_empty_mapping_is_default(self) -> None:
        self.assertEqual(DelegationConfig.from_record({}), DelegationConfig.default())

    def test_block_without_candidates_is_default(self) -> None:
        self.assertEqual(
            DelegationConfig.from_record({"version": DELEGATION_CONFIG_VERSION}),
            DelegationConfig.default(),
        )

    def test_explicit_null_candidates_is_default(self) -> None:
        self.assertEqual(
            DelegationConfig.from_record({"child_candidates": None}),
            DelegationConfig.default(),
        )

    def test_config_is_frozen_and_hashable(self) -> None:
        config = _config({"child_project": CHILD_PROJECT})
        self.assertEqual(config, _config({"child_project": CHILD_PROJECT}))
        self.assertIsInstance(hash(config), int)


class ValidCandidateTest(unittest.TestCase):
    """A public-safe candidate parses into a typed, capability-bearing record."""

    def test_single_candidate_with_capabilities(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation", "review"]}
        )
        (candidate,) = config.child_candidates
        self.assertIsInstance(candidate, ChildCandidate)
        self.assertEqual(candidate.child_project, CHILD_PROJECT)
        self.assertEqual(candidate.capabilities, frozenset({"implementation", "review"}))

    def test_candidate_without_capabilities_defaults_empty(self) -> None:
        (candidate,) = _config({"child_project": CHILD_PROJECT}).child_candidates
        self.assertEqual(candidate.capabilities, frozenset())

    def test_explicit_supported_version_accepted(self) -> None:
        config = DelegationConfig.from_record(
            {
                "version": DELEGATION_CONFIG_VERSION,
                "child_candidates": [{"child_project": CHILD_PROJECT}],
            }
        )
        self.assertEqual(len(config.child_candidates), 1)

    def test_multiple_distinct_candidates(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]},
            {"child_project": "other_project", "capabilities": ["review"]},
        )
        self.assertEqual(len(config.child_candidates), 2)


class SchemaFailClosedTest(unittest.TestCase):
    """Malformed / authority-shaped / private-path-shaped config fails closed."""

    def test_non_mapping_record_rejected(self) -> None:
        for bad in ([], "delegation", 1, ("version", 1)):
            with self.subTest(bad=bad):
                with self.assertRaises(DelegationConfigError):
                    DelegationConfig.from_record(bad)

    def test_unknown_top_level_key_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            DelegationConfig.from_record({"candidates": []})

    def test_policy_knob_not_yet_supported_fails_closed(self) -> None:
        # The enable_*/max_* policy knobs are a separate follow-up loader; until
        # it lands they are unknown keys here, not silently ignored.
        with self.assertRaises(DelegationConfigError):
            DelegationConfig.from_record({"enable_delegated_coordinator": True})

    def test_unsupported_version_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            DelegationConfig.from_record({"version": 2, "child_candidates": []})

    def test_version_bool_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            DelegationConfig.from_record({"version": True})

    def test_candidates_not_a_list_rejected(self) -> None:
        for bad in ({"child_project": CHILD_PROJECT}, "x", 1):
            with self.subTest(bad=bad):
                with self.assertRaises(DelegationConfigError):
                    DelegationConfig.from_record({"child_candidates": bad})

    def test_candidate_not_a_mapping_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            DelegationConfig.from_record({"child_candidates": ["mozyo_bridge"]})

    def test_candidate_unknown_key_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": CHILD_PROJECT, "nickname": "mb"})

    def test_missing_child_project_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"capabilities": ["implementation"]})

    def test_empty_child_project_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": ""})

    def test_non_string_child_project_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": 1})

    def test_duplicate_capability_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": CHILD_PROJECT, "capabilities": ["a", "a"]})

    def test_capabilities_as_bare_string_rejected(self) -> None:
        # A YAML scalar must not be mistaken for a one-item list.
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": CHILD_PROJECT, "capabilities": "implementation"})

    def test_empty_capability_token_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": CHILD_PROJECT, "capabilities": [""]})

    def test_authority_shaped_candidate_key_rejected(self) -> None:
        for key in ("target_pane", "route_override", "close_authority", "owner_approval"):
            with self.subTest(key=key):
                with self.assertRaises(DelegationConfigError):
                    _config({"child_project": CHILD_PROJECT, key: "x"})

    def test_authority_shaped_capability_value_rejected(self) -> None:
        for value in ("owner_approval", "close_authority", "route_override", "target_pane"):
            with self.subTest(value=value):
                with self.assertRaises(DelegationConfigError):
                    _config({"child_project": CHILD_PROJECT, "capabilities": [value]})

    def test_credential_shaped_value_rejected(self) -> None:
        for value in ("api_key", "secret_token", "password"):
            with self.subTest(value=value):
                with self.assertRaises(DelegationConfigError):
                    _config({"child_project": value})

    def test_private_path_shaped_project_rejected(self) -> None:
        for value in ("/abs/proj", "~/proj", "C:proj", "https://h/p", "a/b"):
            with self.subTest(value=value):
                with self.assertRaises(DelegationConfigError):
                    _config({"child_project": value})

    def test_private_path_shaped_capability_rejected(self) -> None:
        with self.assertRaises(DelegationConfigError):
            _config({"child_project": CHILD_PROJECT, "capabilities": ["/abs/cap"]})

    def test_legitimate_role_chain_capability_review_accepted(self) -> None:
        # ``review`` is a shipped role-chain capability and must survive the value
        # screen even though it is an authority-shaped *word*.
        (candidate,) = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["review"]}
        ).child_candidates
        self.assertIn("review", candidate.capabilities)


class ResolveChildCandidateTest(unittest.TestCase):
    """Resolve exactly one candidate; missing / ambiguous fail closed."""

    def test_capability_match_resolves_single(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]}
        )
        resolution = resolve_child_candidate(
            config, child_project=CHILD_PROJECT, capability="implementation"
        )
        self.assertIsInstance(resolution, ChildCandidateResolution)
        self.assertTrue(resolution.is_resolved)
        self.assertEqual(resolution.status, STATUS_RESOLVED)
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_RESOLVED)
        self.assertIsNotNone(resolution.candidate)
        self.assertEqual(resolution.candidate.child_project, CHILD_PROJECT)
        self.assertEqual(resolution.requested_child_project, CHILD_PROJECT)
        self.assertEqual(resolution.requested_capability, "implementation")

    def test_capability_agnostic_request_resolves_single(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]}
        )
        resolution = resolve_child_candidate(config, child_project=CHILD_PROJECT)
        self.assertTrue(resolution.is_resolved)
        self.assertIsNone(resolution.requested_capability)

    def test_operator_resolves_mozyo_bridge_without_injected_route(self) -> None:
        # #12549 core scope: a single declared candidate resolves to an
        # executable-handoff input carrying no route / target / pane / role.
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]}
        )
        resolution = resolve_child_candidate(
            config, child_project=CHILD_PROJECT, capability="implementation"
        )
        self.assertTrue(resolution.is_resolved)
        candidate_fields = vars(resolution.candidate)
        self.assertEqual(set(candidate_fields), {"child_project", "capabilities"})

    def test_unknown_project_is_missing(self) -> None:
        config = _config({"child_project": CHILD_PROJECT})
        resolution = resolve_child_candidate(config, child_project="absent_project")
        self.assertFalse(resolution.is_resolved)
        self.assertEqual(resolution.status, STATUS_MISSING)
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_MISSING)
        self.assertIsNone(resolution.candidate)

    def test_unknown_capability_is_missing(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]}
        )
        resolution = resolve_child_candidate(
            config, child_project=CHILD_PROJECT, capability="review"
        )
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_MISSING)

    def test_capability_agnostic_request_against_capability_only_candidate(self) -> None:
        # A specific-capability request never matches a candidate that does not
        # declare it (fail-closed: empty capabilities means no specific match).
        config = _config({"child_project": CHILD_PROJECT})
        resolution = resolve_child_candidate(
            config, child_project=CHILD_PROJECT, capability="implementation"
        )
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_MISSING)

    def test_empty_config_is_missing(self) -> None:
        resolution = resolve_child_candidate(
            DelegationConfig.default(), child_project=CHILD_PROJECT
        )
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_MISSING)

    def test_two_candidates_same_project_capability_is_ambiguous(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]},
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation", "review"]},
        )
        resolution = resolve_child_candidate(
            config, child_project=CHILD_PROJECT, capability="implementation"
        )
        self.assertFalse(resolution.is_resolved)
        self.assertEqual(resolution.status, STATUS_AMBIGUOUS)
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_AMBIGUOUS)
        self.assertIsNone(resolution.candidate)

    def test_capability_agnostic_request_with_two_candidates_is_ambiguous(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]},
            {"child_project": CHILD_PROJECT, "capabilities": ["review"]},
        )
        resolution = resolve_child_candidate(config, child_project=CHILD_PROJECT)
        self.assertEqual(resolution.diagnostic, CHILD_CANDIDATE_AMBIGUOUS)

    def test_capability_disambiguates_two_candidates(self) -> None:
        config = _config(
            {"child_project": CHILD_PROJECT, "capabilities": ["implementation"]},
            {"child_project": CHILD_PROJECT, "capabilities": ["review"]},
        )
        resolution = resolve_child_candidate(
            config, child_project=CHILD_PROJECT, capability="review"
        )
        self.assertTrue(resolution.is_resolved)
        self.assertEqual(resolution.candidate.capabilities, frozenset({"review"}))

    def test_bad_request_project_fails_closed(self) -> None:
        config = _config({"child_project": CHILD_PROJECT})
        for bad in ("", None, 1):
            with self.subTest(bad=bad):
                with self.assertRaises(DelegationConfigError):
                    resolve_child_candidate(config, child_project=bad)

    def test_bad_request_capability_fails_closed(self) -> None:
        config = _config({"child_project": CHILD_PROJECT})
        for bad in ("", 1):
            with self.subTest(bad=bad):
                with self.assertRaises(DelegationConfigError):
                    resolve_child_candidate(
                        config, child_project=CHILD_PROJECT, capability=bad
                    )


class OracleVocabularyConformanceTest(unittest.TestCase):
    """The resolver diagnostics are exactly the #12547 oracle's reason strings.

    Importing the shipped oracle module pins the contract: if either side renames
    a diagnostic, this fails so the resolver and oracle are updated in lockstep.
    """

    def test_diagnostics_match_shipped_oracle(self) -> None:
        from test_delegated_coordinator_acceptance_oracle import (
            _first_failed_acceptance_reason,
            clean_autonomous_pass_scenario,
        )

        missing = _first_failed_acceptance_reason(
            clean_autonomous_pass_scenario(child_candidate="missing")
        )
        ambiguous = _first_failed_acceptance_reason(
            clean_autonomous_pass_scenario(child_candidate="ambiguous")
        )
        self.assertEqual(missing, CHILD_CANDIDATE_MISSING)
        self.assertEqual(ambiguous, CHILD_CANDIDATE_AMBIGUOUS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
