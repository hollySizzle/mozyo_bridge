"""Tests for role profile template resolution + handoff expansion (Redmine #12388).

US #12388 / Task #12396: the send side resolves a fixed role profile template
(defined by US #12387) and expands the resolved role contract into the durable
delivery record plus a compact single-line pane pointer, persisting the
structured ``role_profile`` / ``profile_source`` / ``profile_version`` fields so
the receiver reads its role contract without guessing a template path. Missing
templates fail closed; omitting the profile is the explicit fallback.
"""

import unittest

from mozyo_bridge.domain.handoff import (
    RedmineAnchor,
    build_delivery_record,
    build_notification_body,
    make_outcome,
)
from mozyo_bridge.domain.role_profile import (
    ROLE_PROFILE_SOURCE,
    ROLE_PROFILE_TOKENS,
    ROLE_PROFILE_VERSION,
    RoleProfileError,
    parse_profile_fields,
    resolve_role_profile,
    template_placeholders,
)


class ResolveRoleProfileTest(unittest.TestCase):
    def test_every_token_resolves_with_pinned_source_and_version(self) -> None:
        for token in ROLE_PROFILE_TOKENS:
            resolution = resolve_role_profile(token, {})
            self.assertEqual(resolution.role_profile, token)
            self.assertEqual(resolution.profile_source, ROLE_PROFILE_SOURCE)
            self.assertEqual(resolution.profile_version, ROLE_PROFILE_VERSION)
            # The template header names the role, so the resolved text always
            # carries the role contract heading.
            self.assertIn(f"# role profile: {token}", resolution.resolved_text)

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError):
            resolve_role_profile("bogus_role", {})

    def test_template_placeholders_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError):
            template_placeholders("bogus_role")

    def test_supplied_fields_substituted_and_unsupplied_left_literal(self) -> None:
        resolution = resolve_role_profile(
            "delegated_coordinator",
            {"parent_project": "alpha", "child_project": "beta"},
        )
        self.assertIn("alpha", resolution.resolved_text)
        self.assertIn("beta", resolution.resolved_text)
        # Unsupplied placeholders stay as literal `<name>` tokens (explicit
        # partial-resolution fallback) and are reported, not silently dropped.
        self.assertIn("<parent_issue>", resolution.resolved_text)
        self.assertIn("parent_issue", resolution.unresolved_placeholders)
        self.assertIn("redmine_project", resolution.unresolved_placeholders)
        # Substituted placeholders are not reported as unresolved.
        self.assertNotIn("parent_project", resolution.unresolved_placeholders)

    def test_fully_supplied_fields_have_no_unresolved(self) -> None:
        fields = {name: f"val_{name}" for name in template_placeholders("implementation_worker")}
        resolution = resolve_role_profile("implementation_worker", fields)
        self.assertEqual(resolution.unresolved_placeholders, ())
        self.assertNotIn("<", resolution.resolved_text.split("\n", 1)[1])

    def test_empty_field_value_treated_as_unresolved(self) -> None:
        resolution = resolve_role_profile("implementation_gateway", {"lane": ""})
        self.assertIn("lane", resolution.unresolved_placeholders)

    def test_structured_dict_is_free_text_free(self) -> None:
        resolution = resolve_role_profile(
            "delegated_coordinator", {"parent_project": "secret-value"}
        )
        structured = resolution.to_structured_dict()
        self.assertEqual(
            set(structured),
            {"role_profile", "profile_source", "profile_version", "unresolved_placeholders"},
        )
        # The substituted free-text value lives only in resolved_text, never in
        # the structured pointer payload.
        self.assertNotIn("secret-value", repr(structured))

    def test_pointer_clause_is_single_line(self) -> None:
        resolution = resolve_role_profile("coordinator", {"project": "alpha"})
        clause = resolution.pointer_clause()
        self.assertNotIn("\n", clause)
        self.assertIn("coordinator", clause)
        self.assertIn(ROLE_PROFILE_SOURCE, clause)
        self.assertIn(ROLE_PROFILE_VERSION, clause)


class ParseProfileFieldsTest(unittest.TestCase):
    def test_parses_key_value_pairs(self) -> None:
        self.assertEqual(
            parse_profile_fields(["a=b", "c=d"]), {"a": "b", "c": "d"}
        )

    def test_value_may_contain_equals(self) -> None:
        self.assertEqual(parse_profile_fields(["url=k=v"]), {"url": "k=v"})

    def test_none_yields_empty(self) -> None:
        self.assertEqual(parse_profile_fields(None), {})

    def test_missing_equals_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError):
            parse_profile_fields(["noequals"])

    def test_empty_key_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError):
            parse_profile_fields(["=v"])


class NotificationBodyRoleProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor = RedmineAnchor(issue="12396", journal="63130")

    def test_no_role_profile_body_unchanged(self) -> None:
        body = build_notification_body(self.anchor, "implementation_request", None, "claude")
        self.assertNotIn("role profile:", body)

    def test_role_profile_appends_single_line_clause(self) -> None:
        resolution = resolve_role_profile("implementation_worker", {"lane": "mozyo_bridge-12388"})
        body = build_notification_body(
            self.anchor,
            "implementation_request",
            None,
            "claude",
            role_profile=resolution,
        )
        # The body is delivered via a single `tmux send-keys -l`, so it must stay
        # single-line even with the role-profile clause appended.
        self.assertNotIn("\n", body)
        self.assertIn("role profile: implementation_worker", body)


class OutcomeAndRecordRoleProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor = RedmineAnchor(issue="12396", journal="63130")
        self.resolution = resolve_role_profile(
            "delegated_coordinator",
            {"parent_project": "alpha", "child_project": "beta"},
        )

    def _build(self, role_profile=None):
        return make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%129",
            anchor=self.anchor,
            mode="standard",
            kind="implementation_request",
            notification_marker="[mozyo:handoff:...]",
            role_profile=role_profile,
        )

    def test_outcome_carries_structured_fields(self) -> None:
        outcome = self._build(self.resolution)
        self.assertIsNotNone(outcome.role_profile)
        self.assertEqual(outcome.role_profile["role_profile"], "delegated_coordinator")
        self.assertEqual(outcome.role_profile["profile_source"], ROLE_PROFILE_SOURCE)
        self.assertEqual(outcome.role_profile["profile_version"], ROLE_PROFILE_VERSION)
        # JSON round-trips (frozen dataclass asdict path).
        self.assertIn("delegated_coordinator", outcome.to_json())

    def test_outcome_none_when_no_profile(self) -> None:
        self.assertIsNone(self._build(None).role_profile)

    def test_record_renders_structured_pointer(self) -> None:
        record = build_delivery_record(self._build(self.resolution))
        self.assertIn("- Role profile: `delegated_coordinator`", record)
        self.assertIn(ROLE_PROFILE_SOURCE, record)
        self.assertIn("unresolved fields:", record)

    def test_record_dash_when_no_profile(self) -> None:
        record = build_delivery_record(self._build(None))
        self.assertIn("- Role profile: —", record)

    def test_record_includes_resolved_contract_when_supplied(self) -> None:
        record = build_delivery_record(
            self._build(self.resolution),
            role_profile_contract=self.resolution.resolved_text,
        )
        self.assertIn("Resolved role profile contract:", record)
        self.assertIn("# role profile: delegated_coordinator", record)
        self.assertIn("alpha", record)

    def test_record_omits_contract_body_when_not_supplied(self) -> None:
        # The opt-in auto-persist path omits the free-text body (mirrors
        # `--record-command`): structured pointer renders, contract block does not.
        record = build_delivery_record(self._build(self.resolution))
        self.assertIn("- Role profile: `delegated_coordinator`", record)
        self.assertNotIn("Resolved role profile contract:", record)


if __name__ == "__main__":
    unittest.main()
