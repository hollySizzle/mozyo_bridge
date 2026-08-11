"""Pure-decision tests for nested-workspace alias / launch-disable (#15190)."""

from __future__ import annotations

import unittest
from dataclasses import replace

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    ALIAS_SCHEMA_VERSION,
    GIT_BINDING_DIFFERENT,
    GIT_BINDING_NOT_MEASURABLE,
    GIT_BINDING_SAME,
    GIT_BINDING_UNAVAILABLE,
    MODE_ALIAS,
    MODE_DISABLED,
    REASON_ALIAS_CYCLE,
    REASON_CROSS_REPOSITORY,
    REASON_DECLARATION_INVALID,
    REASON_GIT_BINDING_UNAVAILABLE,
    REASON_TARGET_IDENTITY_MISMATCH,
    REASON_TARGET_IDENTITY_UNRESOLVED,
    REASON_TARGET_MISSING,
    REASON_TARGET_NOT_ANCESTOR,
    REASON_TARGET_NOT_DECLARED,
    REASON_UNSUPPORTED_SCHEMA,
    STATE_ALIASED,
    STATE_LAUNCH_DISABLED,
    STATE_NO_DECLARATION,
    STATE_REFUSED,
    AliasResolution,
    AliasTargetObservation,
    WorkspaceAliasDeclaration,
    build_alias_resolution,
    parse_declaration,
)


SOURCE = "/repo/Source/rails"
CANONICAL = "/repo"
CANONICAL_ID = "ddd145b984ea4bc6ba842f72d4d4161f"


def _declaration(**changes) -> WorkspaceAliasDeclaration:
    values = {
        "mode": MODE_ALIAS,
        "canonical_path": CANONICAL,
        "canonical_workspace_id": CANONICAL_ID,
    }
    values.update(changes)
    return WorkspaceAliasDeclaration(**values)


def _target(**changes) -> AliasTargetObservation:
    values = {
        "exists": True,
        "is_dir": True,
        "workspace_id": CANONICAL_ID,
        "git_binding": GIT_BINDING_SAME,
        "is_ancestor_of_source": True,
        "declares_alias": False,
    }
    values.update(changes)
    return AliasTargetObservation(**values)


def _resolve(**changes) -> AliasResolution:
    kwargs = {
        "source_root": SOURCE,
        "declaration": _declaration(),
        "target": _target(),
    }
    kwargs.update(changes)
    return build_alias_resolution(**kwargs)


class NoDeclarationTests(unittest.TestCase):
    def test_absent_declaration_keeps_the_requested_root(self) -> None:
        """The overwhelmingly common path must stay byte-for-byte unchanged."""
        resolution = build_alias_resolution(source_root=SOURCE, declaration=None)
        self.assertEqual(resolution.state, STATE_NO_DECLARATION)
        self.assertEqual(resolution.launch_root, SOURCE)
        self.assertTrue(resolution.ok)
        self.assertFalse(resolution.redirected)


class AliasAdoptionTests(unittest.TestCase):
    def test_verified_alias_redirects_to_the_canonical_root(self) -> None:
        resolution = _resolve()
        self.assertEqual(resolution.state, STATE_ALIASED)
        self.assertEqual(resolution.launch_root, CANONICAL)
        self.assertTrue(resolution.ok)
        self.assertTrue(resolution.redirected)

    def test_non_git_pair_is_admitted_on_containment_alone(self) -> None:
        """A scaffolded non-git workspace nested in another (#11301) is legitimate."""
        resolution = _resolve(
            target=_target(git_binding=GIT_BINDING_NOT_MEASURABLE)
        )
        self.assertEqual(resolution.state, STATE_ALIASED)


class LaunchDisabledTests(unittest.TestCase):
    def test_disabled_is_zero_launch_with_a_fixed_reason(self) -> None:
        resolution = build_alias_resolution(
            source_root=SOURCE,
            declaration=WorkspaceAliasDeclaration(
                mode=MODE_DISABLED, reason="no canonical parent"
            ),
        )
        self.assertEqual(resolution.state, STATE_LAUNCH_DISABLED)
        self.assertEqual(resolution.reason, MODE_DISABLED)
        self.assertFalse(resolution.ok)
        self.assertEqual(resolution.launch_root, "")


class FailClosedTests(unittest.TestCase):
    """Every verification failure is zero-launch with its own typed reason.

    The shared assertion in each case is that ``launch_root`` stays empty: the
    defect this rail removes is precisely the fallback "launch at the nested
    root anyway", so no refusal may leave a usable root behind.
    """

    def _assert_refused(self, resolution: AliasResolution, reason: str) -> None:
        self.assertEqual(resolution.state, STATE_REFUSED)
        self.assertEqual(resolution.reason, reason)
        self.assertFalse(resolution.ok)
        self.assertEqual(resolution.launch_root, "")

    def test_missing_target(self) -> None:
        self._assert_refused(
            _resolve(target=_target(exists=False, is_dir=False)),
            REASON_TARGET_MISSING,
        )

    def test_cross_repository_target(self) -> None:
        """A submodule sits inside its superproject's tree but is another repo."""
        self._assert_refused(
            _resolve(target=_target(git_binding=GIT_BINDING_DIFFERENT)),
            REASON_CROSS_REPOSITORY,
        )

    def test_unavailable_git_binding_is_its_own_typed_refusal(self) -> None:
        self._assert_refused(
            _resolve(target=_target(git_binding=GIT_BINDING_UNAVAILABLE)),
            REASON_GIT_BINDING_UNAVAILABLE,
        )

    def test_target_that_does_not_contain_the_source(self) -> None:
        self._assert_refused(
            _resolve(target=_target(is_ancestor_of_source=False)),
            REASON_TARGET_NOT_ANCESTOR,
        )

    def test_target_without_durable_identity(self) -> None:
        self._assert_refused(
            _resolve(target=_target(workspace_id="")),
            REASON_TARGET_IDENTITY_UNRESOLVED,
        )

    def test_ambiguous_identity_drift(self) -> None:
        """Same path, re-minted identity: a different workspace, so fail closed."""
        self._assert_refused(
            _resolve(target=_target(workspace_id="0" * 32)),
            REASON_TARGET_IDENTITY_MISMATCH,
        )

    def test_alias_chain_is_refused(self) -> None:
        self._assert_refused(
            _resolve(target=_target(declares_alias=True)),
            REASON_ALIAS_CYCLE,
        )

    def test_missing_target_observation(self) -> None:
        self._assert_refused(
            _resolve(target=None),
            REASON_TARGET_IDENTITY_UNRESOLVED,
        )

    def test_invalid_source_root(self) -> None:
        self._assert_refused(
            _resolve(source_root=""),
            REASON_DECLARATION_INVALID,
        )


class ParseTests(unittest.TestCase):
    def _refusal_reason(self, raw: object) -> str:
        parsed = parse_declaration(raw)
        self.assertIsInstance(parsed, AliasResolution)
        return parsed.reason

    def test_round_trip_alias(self) -> None:
        parsed = parse_declaration(_declaration(reason="folded").as_payload())
        self.assertIsInstance(parsed, WorkspaceAliasDeclaration)
        self.assertEqual(parsed.mode, MODE_ALIAS)
        self.assertEqual(parsed.canonical_path, CANONICAL)
        self.assertEqual(parsed.canonical_workspace_id, CANONICAL_ID)
        self.assertEqual(parsed.reason, "folded")

    def test_round_trip_disabled(self) -> None:
        parsed = parse_declaration(
            WorkspaceAliasDeclaration(mode=MODE_DISABLED).as_payload()
        )
        self.assertIsInstance(parsed, WorkspaceAliasDeclaration)
        self.assertEqual(parsed.mode, MODE_DISABLED)

    def test_unknown_schema_version_is_refused_not_guessed(self) -> None:
        payload = _declaration().as_payload()
        payload["schema_version"] = ALIAS_SCHEMA_VERSION + 1
        self.assertEqual(self._refusal_reason(payload), REASON_UNSUPPORTED_SCHEMA)

    def test_non_mapping_is_refused(self) -> None:
        self.assertEqual(self._refusal_reason(["not", "a", "mapping"]),
                         REASON_DECLARATION_INVALID)

    def test_unknown_mode_is_refused(self) -> None:
        payload = _declaration().as_payload()
        payload["mode"] = "launch-anyway"
        self.assertEqual(self._refusal_reason(payload), REASON_DECLARATION_INVALID)

    def test_alias_without_target_is_refused(self) -> None:
        payload = _declaration().as_payload()
        payload["canonical_path"] = ""
        self.assertEqual(self._refusal_reason(payload), REASON_TARGET_NOT_DECLARED)

    def test_alias_without_identity_binding_is_refused(self) -> None:
        payload = _declaration().as_payload()
        payload["canonical_workspace_id"] = ""
        self.assertEqual(self._refusal_reason(payload), REASON_TARGET_NOT_DECLARED)

    def test_self_alias_is_refused(self) -> None:
        resolution = build_alias_resolution(
            source_root=SOURCE,
            declaration=replace(_declaration(), canonical_path=SOURCE),
            target=_target(is_ancestor_of_source=False),
        )
        self.assertEqual(resolution.state, STATE_REFUSED)
        self.assertEqual(resolution.launch_root, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
