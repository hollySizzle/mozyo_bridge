"""Unit tests for the ``onboarding_seed`` profile schema (Redmine #15744).

Subject under test: the pure declaration schema
``...f_160_provider_registry.domain.agent_provider_onboarding_seed``. Isolated and
I/O-free — the schema decides what a profile MAY declare; whether a document is
subsequently written is the application layer's concern and is covered separately.

The assertions worth reading closely are the boundary ones. This block sits next to
``startup_blockers``, which declares screens a launch must refuse to send into, and the
whole reason a *seed* is admissible where an *auto-answer* is not is that a seed can
only ever install a first-run UI default. That is enforced here, not by convention, so
these tests are the executable form of the #13760 境界 restated by #15744.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_onboarding_seed import (  # noqa: E501
    MAX_SEED_DOCUMENTS,
    MAX_SEED_KEYS,
    MAX_SEED_VALUE_LEN,
    ONBOARDING_SEED_MIN_VERSION,
    OnboardingSeedDeclaration,
    OnboardingSeedDocument,
    parse_onboarding_seed,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
    AgentProviderProfileError,
)


def _document(**overrides) -> dict:
    record = {
        "id": "home_document",
        "base_env": "PROVIDER_CONFIG_DIR",
        "base_home_relative": "",
        "filename": ".provider.json",
        "create_when_absent": True,
    }
    record.update(overrides)
    return record


def _seed(**overrides) -> dict:
    record = {
        "documents": [_document()],
        "completion_keys": {"hasCompletedOnboarding": True},
    }
    record.update(overrides)
    return record


class OnboardingSeedKeyBoundaryTests(unittest.TestCase):
    """A seed may install a UI default and nothing that decides trust or identity."""

    def test_rejects_credential_shaped_keys(self) -> None:
        # Each of these is a real config key family a provider keeps beside its
        # onboarding flag. Installing one would mean a managed launch established an
        # authentication state the operator never did.
        for key in (
            "oauthAccount",
            "primaryApiKey",
            "customApiKeyResponses",
            "authToken",
            "userSecret",
            "devicePassword",
            "credentialStore",
        ):
            with self.subTest(key=key):
                with self.assertRaises(AgentProviderProfileError) as caught:
                    OnboardingSeedDeclaration.from_record(
                        _seed(completion_keys={key: "x"}), provider_id="p"
                    )
                self.assertIn("operator-resolved startup blocker", str(caught.exception))

    def test_rejects_trust_and_permission_acceptance_keys(self) -> None:
        # The #13760 boundary: auto-accepting a trust prompt is out of scope, and that
        # holds whether the acceptance arrives as a keystroke or as committed data.
        for key in (
            "hasTrustDialogAccepted",
            "trustedWorkspaces",
            "permissionMode",
            "bypassPermissions",
        ):
            with self.subTest(key=key):
                with self.assertRaises(AgentProviderProfileError):
                    OnboardingSeedDeclaration.from_record(
                        _seed(completion_keys={key: True}), provider_id="p"
                    )

    def test_rejects_the_per_project_sub_document_container(self) -> None:
        # `projects` carries none of the forbidden substrings but is where a provider
        # keeps per-workspace trust acceptances, so the container itself is refused.
        with self.assertRaises(AgentProviderProfileError) as caught:
            OnboardingSeedDeclaration.from_record(
                _seed(completion_keys={"projects": "x"}), provider_id="p"
            )
        self.assertIn("per-project sub-document", str(caught.exception))

    def test_rejects_keys_that_address_a_nested_path(self) -> None:
        for key in ("projects.foo", "a/b", "a b", "1leading"):
            with self.subTest(key=key):
                with self.assertRaises(AgentProviderProfileError):
                    OnboardingSeedDeclaration.from_record(
                        _seed(completion_keys={key: True}), provider_id="p"
                    )

    def test_accepts_a_plain_first_run_ui_default(self) -> None:
        declaration = OnboardingSeedDeclaration.from_record(
            _seed(completion_keys={"hasCompletedOnboarding": True, "theme": "dark"}),
            provider_id="p",
        )
        self.assertEqual(
            declaration.completion_key_map,
            {"hasCompletedOnboarding": True, "theme": "dark"},
        )


class OnboardingSeedValueTests(unittest.TestCase):
    """Values are scalars, and a declared boolean stays a boolean."""

    def test_rejects_nested_values(self) -> None:
        # A mapping value is how a seed would otherwise reach into a sub-document the
        # key fence is guarding.
        for value in ({"nested": True}, ["a"], 1.5, None):
            with self.subTest(value=value):
                with self.assertRaises(AgentProviderProfileError):
                    OnboardingSeedDeclaration.from_record(
                        _seed(completion_keys={"someDefault": value}), provider_id="p"
                    )

    def test_boolean_is_not_narrowed_to_int(self) -> None:
        # `bool` is an `int` subclass, and the difference is observable in the JSON a
        # seed writes: a provider testing `flag === true` would reject `1`.
        declaration = OnboardingSeedDeclaration.from_record(
            _seed(completion_keys={"hasCompletedOnboarding": True}), provider_id="p"
        )
        value = declaration.completion_key_map["hasCompletedOnboarding"]
        self.assertIs(value, True)
        self.assertIsInstance(value, bool)

    def test_rejects_an_overlong_string_value(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(completion_keys={"theme": "x" * (MAX_SEED_VALUE_LEN + 1)}),
                provider_id="p",
            )


class OnboardingSeedDocumentTests(unittest.TestCase):
    """A document is declared as components; committed data never names a host path."""

    def test_rejects_an_absolute_or_traversing_base(self) -> None:
        for relative in ("/etc", "../..", "a/../../b", "C:foo"):
            with self.subTest(relative=relative):
                with self.assertRaises(AgentProviderProfileError):
                    OnboardingSeedDocument.from_record(
                        _document(base_home_relative=relative), provider_id="p"
                    )

    def test_rejects_a_filename_that_is_a_path(self) -> None:
        for filename in ("a/b.json", "..", ".", "sub\\b.json"):
            with self.subTest(filename=filename):
                with self.assertRaises(AgentProviderProfileError):
                    OnboardingSeedDocument.from_record(
                        _document(filename=filename), provider_id="p"
                    )

    def test_rejects_a_base_env_that_is_a_value_not_a_name(self) -> None:
        # The `executable.env_override` posture: committed data names the variable and
        # the trusted environment supplies what it points at.
        with self.assertRaises(AgentProviderProfileError) as caught:
            OnboardingSeedDocument.from_record(
                _document(base_env="/home/someone/.config"), provider_id="p"
            )
        self.assertIn("environment variable NAME", str(caught.exception))

    def test_rejects_unknown_document_keys(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDocument.from_record(
                _document(answer="yes"), provider_id="p"
            )

    def test_requires_exactly_one_creatable_document(self) -> None:
        both = [
            _document(id="a", filename="a.json", create_when_absent=True),
            _document(id="b", filename="b.json", create_when_absent=True),
        ]
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(documents=both), provider_id="p"
            )
        neither = [_document(create_when_absent=False)]
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(documents=neither), provider_id="p"
            )

    def test_rejects_duplicate_document_ids(self) -> None:
        documents = [
            _document(id="same", filename="a.json", create_when_absent=True),
            _document(id="same", filename="b.json", create_when_absent=False),
        ]
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(documents=documents), provider_id="p"
            )


class OnboardingSeedBoundsTests(unittest.TestCase):
    """Bounds keep the block a declaration rather than a filesystem search."""

    def test_rejects_too_many_documents(self) -> None:
        documents = [
            _document(id=f"d{index}", filename=f"{index}.json", create_when_absent=index == 0)
            for index in range(MAX_SEED_DOCUMENTS + 1)
        ]
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(documents=documents), provider_id="p"
            )

    def test_rejects_too_many_completion_keys(self) -> None:
        keys = {f"someDefault{index}": True for index in range(MAX_SEED_KEYS + 1)}
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(completion_keys=keys), provider_id="p"
            )

    def test_rejects_an_empty_declaration(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(documents=[]), provider_id="p"
            )
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(completion_keys={}), provider_id="p"
            )


class OnboardingSeedVersionLockStepTests(unittest.TestCase):
    """A field is honored only by an artifact whose version says it has that field."""

    def test_absent_block_parses_to_none_on_every_version(self) -> None:
        for version in ("1", "2", "3", ONBOARDING_SEED_MIN_VERSION):
            with self.subTest(version=version):
                self.assertIsNone(
                    parse_onboarding_seed(
                        {"protocol": "interactive_cli_tui"},
                        provider_id="p",
                        schema_version=version,
                    )
                )

    def test_rejects_the_block_on_a_pre_v4_artifact(self) -> None:
        for version in ("1", "2", "3"):
            with self.subTest(version=version):
                with self.assertRaises(AgentProviderProfileError) as caught:
                    parse_onboarding_seed(
                        {"onboarding_seed": _seed()},
                        provider_id="p",
                        schema_version=version,
                    )
                self.assertIn(ONBOARDING_SEED_MIN_VERSION, str(caught.exception))

    def test_parses_the_block_on_v4(self) -> None:
        declaration = parse_onboarding_seed(
            {"onboarding_seed": _seed()},
            provider_id="p",
            schema_version=ONBOARDING_SEED_MIN_VERSION,
        )
        self.assertIsInstance(declaration, OnboardingSeedDeclaration)
        self.assertEqual(declaration.creatable_document.document_id, "home_document")

    def test_rejects_unknown_seed_keys(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            OnboardingSeedDeclaration.from_record(
                _seed(answer_screens=True), provider_id="p"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
