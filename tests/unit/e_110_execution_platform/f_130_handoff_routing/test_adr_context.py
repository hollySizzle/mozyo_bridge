"""Unit tests for the pure ADR context pointer (Redmine #15722).

Pure-bucket: no filesystem, no tempfile, no subprocess — the repo read lives in
``application/adr_context_resolution.py`` and is covered by the integration test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.adr_context import (
    ADR_READ_OBLIGATION,
    BINDING_STATUSES,
    STATUS_ACTIVE,
    STATUS_PROPOSED,
    STATUS_SUPERSEDED,
    STATUS_UNKNOWN,
    AdrContextError,
    AdrContextPointer,
    AdrRef,
    adr_context_from_payload,
    make_adr_ref,
    resolvable_paths_for,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    resolve_role_profile,
    with_adr_context,
)

INDEX = "vibes/docs/adr/README.md"


def _pointer(*refs: AdrRef) -> AdrContextPointer:
    return AdrContextPointer(
        index_canonical_path=INDEX,
        index_resolvable_paths=resolvable_paths_for(INDEX),
        refs=refs,
    )


ACTIVE_REF = make_adr_ref("adr-0001", "vibes/docs/adr/adr-0001-adr-practice.md", "active")
PROPOSED_REF = make_adr_ref(
    "adr-0011",
    "vibes/docs/adr/adr-0011-three-layer-responsibility-division.md",
    "proposed (owner ratify 待ち)",
)


class NormalizeAdrStatusTest(unittest.TestCase):
    def test_known_statuses_pass_through(self) -> None:
        self.assertEqual(make_adr_ref("adr-0001", INDEX, "active").status, STATUS_ACTIVE)
        self.assertEqual(
            make_adr_ref("adr-0001", INDEX, "proposed").status, STATUS_PROPOSED
        )
        self.assertEqual(
            make_adr_ref("adr-0001", INDEX, "superseded").status, STATUS_SUPERSEDED
        )

    def test_trailing_qualifier_does_not_change_the_status(self) -> None:
        # The two real qualifier shapes the ADR format admits.
        self.assertEqual(
            make_adr_ref("adr-0011", INDEX, "proposed (owner ratify 待ち。…)").status,
            STATUS_PROPOSED,
        )
        self.assertEqual(
            make_adr_ref("adr-0002", INDEX, "superseded (by ADR-0007)").status,
            STATUS_SUPERSEDED,
        )

    def test_unrecognised_status_becomes_unknown_and_never_active(self) -> None:
        for raw in (
            "",
            "   ",
            "draft",
            "ACTIVE-ish",
            "accepted",
            None,
            42,
            "not active",
        ):
            with self.subTest(raw=raw):
                ref = make_adr_ref("adr-0009", INDEX, raw)
                self.assertEqual(ref.status, STATUS_UNKNOWN)
                self.assertFalse(ref.is_binding)

    def test_non_literal_active_is_unknown_and_never_binding(self) -> None:
        # Review j#108679 finding_noncanonicalstatuspromotion: only the exact
        # literal `active` token binds; case variants are unknown, not promoted.
        for raw in ("Active", "ACTIVE", "aCtIvE"):
            ref = make_adr_ref("adr-0001", INDEX, raw)
            self.assertEqual(ref.status, STATUS_UNKNOWN)
            self.assertFalse(ref.is_binding)
        self.assertEqual(make_adr_ref("adr-0001", INDEX, "active").status, STATUS_ACTIVE)
        self.assertEqual(BINDING_STATUSES, frozenset({STATUS_ACTIVE}))


class AdrRefTest(unittest.TestCase):
    def test_resolvable_paths_carry_canonical_and_monorepo_nested_forms(self) -> None:
        ref = make_adr_ref("adr-0001", "vibes/docs/adr/adr-0001-adr-practice.md", "active")
        self.assertEqual(
            ref.resolvable_paths,
            (
                "vibes/docs/adr/adr-0001-adr-practice.md",
                "projects/giken-3800-mozyo-bridge/vibes/docs/adr/adr-0001-adr-practice.md",
            ),
        )

    def test_blank_fields_fail_closed(self) -> None:
        with self.assertRaises(AdrContextError):
            make_adr_ref("", "vibes/docs/adr/x.md", "active")
        with self.assertRaises(AdrContextError):
            make_adr_ref("adr-0001", "  ", "active")

    def test_hand_built_ref_cannot_smuggle_an_unvetted_status(self) -> None:
        with self.assertRaises(AdrContextError):
            AdrRef(
                adr_id="adr-0001",
                canonical_path="vibes/docs/adr/adr-0001-adr-practice.md",
                resolvable_paths=("vibes/docs/adr/adr-0001-adr-practice.md",),
                status="ratified",
            )


class AdrContextPointerTest(unittest.TestCase):
    def test_binding_partition_keeps_proposed_out_of_the_rules(self) -> None:
        pointer = _pointer(ACTIVE_REF, PROPOSED_REF)
        self.assertEqual(pointer.binding_refs(), (ACTIVE_REF,))
        self.assertEqual(pointer.non_binding_refs(), (PROPOSED_REF,))

    def test_structured_dict_reports_status_and_binding_per_adr(self) -> None:
        payload = _pointer(ACTIVE_REF, PROPOSED_REF).to_structured_dict()
        self.assertEqual(payload["index_canonical_path"], INDEX)
        self.assertEqual(payload["read_obligation"], ADR_READ_OBLIGATION)
        self.assertEqual(payload["binding_statuses"], [STATUS_ACTIVE])
        by_id = {ref["adr_id"]: ref for ref in payload["refs"]}  # type: ignore[index]
        self.assertEqual(by_id["adr-0001"]["status"], STATUS_ACTIVE)
        self.assertIs(by_id["adr-0001"]["binding"], True)
        self.assertEqual(by_id["adr-0011"]["status"], STATUS_PROPOSED)
        self.assertIs(by_id["adr-0011"]["binding"], False)

    def test_pointer_clause_is_single_line_and_states_the_binding_split(self) -> None:
        clause = _pointer(ACTIVE_REF, PROPOSED_REF).pointer_clause()
        self.assertNotIn("\n", clause)
        self.assertIn(INDEX, clause)
        self.assertIn("1 active (binding)", clause)
        self.assertIn("1 non-active (not binding)", clause)

    def test_record_lines_label_non_active_adrs_as_not_binding(self) -> None:
        lines = _pointer(ACTIVE_REF, PROPOSED_REF).record_lines()
        rendered = "\n".join(lines)
        self.assertIn("`adr-0001`", rendered)
        self.assertIn("NOT binding", rendered)
        self.assertIn("`adr-0011` status `proposed`", rendered)

    def test_contract_lines_mark_every_non_active_entry(self) -> None:
        rendered = "\n".join(_pointer(ACTIVE_REF, PROPOSED_REF).contract_lines())
        self.assertIn("- active: adr-0001", rendered)
        self.assertIn("- proposed (NOT binding): adr-0011", rendered)

    def test_empty_ref_set_is_allowed(self) -> None:
        pointer = _pointer()
        self.assertEqual(pointer.refs, ())
        self.assertIn("0 active (binding)", pointer.pointer_clause())

    def test_duplicate_adr_id_fails_closed(self) -> None:
        with self.assertRaises(AdrContextError):
            _pointer(ACTIVE_REF, ACTIVE_REF)

    def test_blank_index_fails_closed(self) -> None:
        with self.assertRaises(AdrContextError):
            AdrContextPointer(
                index_canonical_path="  ",
                index_resolvable_paths=(INDEX,),
                refs=(),
            )


class AdrContextPayloadRoundTripTest(unittest.TestCase):
    def test_round_trip_preserves_ids_paths_and_statuses(self) -> None:
        original = _pointer(ACTIVE_REF, PROPOSED_REF)
        rebuilt = adr_context_from_payload(original.to_structured_dict())
        self.assertEqual(rebuilt, original)

    def test_binding_flag_in_a_payload_cannot_promote_a_proposed_adr(self) -> None:
        payload = _pointer(PROPOSED_REF).to_structured_dict()
        payload["refs"][0]["binding"] = True  # type: ignore[index]
        rebuilt = adr_context_from_payload(payload)
        self.assertEqual(rebuilt.refs[0].status, STATUS_PROPOSED)
        self.assertFalse(rebuilt.refs[0].is_binding)
        self.assertEqual(rebuilt.binding_refs(), ())

    def test_missing_field_fails_closed(self) -> None:
        payload = _pointer(ACTIVE_REF).to_structured_dict()
        del payload["refs"]
        with self.assertRaises(AdrContextError):
            adr_context_from_payload(payload)


class RoleProfileAdrContextAttachmentTest(unittest.TestCase):
    """The additive attachment seam on the role-profile resolution (#15722 AC1/AC3)."""

    def _resolution(self):
        return resolve_role_profile(
            "implementation_worker",
            {
                "lane": "issue_15722_adr_context_resolution",
                "durable_anchor": "#15722 j#108275",
                "gateway_callback_target": "gateway",
            },
        )

    def test_without_adr_context_the_payload_is_unchanged(self) -> None:
        base = self._resolution()
        self.assertIs(with_adr_context(base, None), base)
        # Review j#108679 finding_nullkeybreaksnoadrcompat: the no-ADR payload
        # carries no `adr_context` key at all (byte-identical to pre-#15722).
        self.assertNotIn("adr_context", base.to_structured_dict())
        self.assertEqual(base.record_contract_text(), base.resolved_text)
        self.assertNotIn("adr context:", base.pointer_clause())

    def test_attached_context_reaches_body_record_and_structured_payload(self) -> None:
        attached = with_adr_context(self._resolution(), _pointer(ACTIVE_REF, PROPOSED_REF))
        clause = attached.pointer_clause()
        self.assertNotIn("\n", clause)
        self.assertIn("adr context: index vibes/docs/adr/README.md", clause)
        self.assertIn(
            "# ADR context (repo-local, resolved at send time",
            attached.record_contract_text(),
        )
        self.assertEqual(
            attached.to_structured_dict()["adr_context"],
            _pointer(ACTIVE_REF, PROPOSED_REF).to_structured_dict(),
        )

    def test_attachment_does_not_touch_the_versioned_template_text(self) -> None:
        base = self._resolution()
        attached = with_adr_context(base, _pointer(ACTIVE_REF))
        self.assertEqual(attached.resolved_text, base.resolved_text)
        self.assertEqual(attached.profile_version, base.profile_version)
        self.assertEqual(attached.profile_source, base.profile_source)


if __name__ == "__main__":  # pragma: no cover - unittest entrypoint
    unittest.main()
