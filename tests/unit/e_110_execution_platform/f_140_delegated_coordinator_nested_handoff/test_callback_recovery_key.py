"""The recovery admission key: collision, domain separation, wire round-trip (#13910 j#80984).

Review focus "stable key の collision/domain separation". These are pure tests: the key is a value,
and its whole job is that distinct actions cannot share an identity. A collision here would not
raise — it would silently no-op a DIFFERENT recovery as a "duplicate", which is a lost action nobody
can detect afterwards. So the boundary cases are asserted directly rather than inferred.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_recovery_key import (
    LOOKUP_ENTRY_ABSENT,
    LOOKUP_ENTRY_AMBIGUOUS,
    LOOKUP_MARKER_ABSENT,
    LOOKUP_MARKER_AMBIGUOUS,
    LOOKUP_MARKER_MALFORMED,
    RECOVERY_KEY_SCHEMA_VERSION,
    RecoveryAdmissionKey,
    RecoveryKeyError,
    render_recovery_action_marker,
    resolve_recovery_action_key,
)

_BASE = dict(
    recovery_action_journal="80500",
    original_dispatch_anchor="79990",
    workspace_id="ws-1",
    lane_id="lane_a",
    lane_generation="1",
    route_identity="claude-worker-1",
    receiver_identity="codex",
    action_kind="callback_sweep_recovery",
)

_MARKER_ARGS = dict(
    original_dispatch_anchor="79990",
    workspace_id="ws-1",
    lane_id="lane_a",
    lane_generation="1",
    route_identity="claude-worker-1",
    receiver_identity="codex",
    action_kind="callback_sweep_recovery",
)


class _Entry:
    """The duck-typed durable entry the readers consume (journal_id + notes)."""

    def __init__(self, journal_id, notes):
        self.journal_id = journal_id
        self.notes = notes


def key(**overrides):
    return RecoveryAdmissionKey(**{**_BASE, **overrides})


class RecoveryKeyIdentityTests(unittest.TestCase):
    def test_same_facts_yield_the_same_digest(self):
        """Stability: the key is content-addressed, so a re-derivation must land on the same row."""
        self.assertEqual(key().digest(), key().digest())

    def test_every_field_changes_the_digest(self):
        """No field is decorative: each one narrows WHICH action this is, so each must bind."""
        base = key().digest()
        for field in _BASE:
            with self.subTest(field=field):
                self.assertNotEqual(
                    base, key(**{field: _BASE[field] + "x"}).digest(),
                    f"{field} does not participate in the digest: two distinct recovery actions "
                    f"would share one identity and the second would be dropped as a duplicate",
                )

    def test_field_boundary_shift_does_not_collide(self):
        """The collision a plain delimiter join would produce (the reason for length prefixes).

        ``lane="a"`` + ``route="b-c"`` and ``lane="a-b"`` + ``route="c"`` concatenate identically
        under naive joining. Length-prefixed encoding must keep them distinct.
        """
        left = key(lane_id="a", route_identity="b-c")
        right = key(lane_id="a-b", route_identity="c")
        self.assertNotEqual(left.canonical_encoding(), right.canonical_encoding())
        self.assertNotEqual(left.digest(), right.digest())

    def test_domain_tag_prefixes_every_encoding(self):
        """Domain separation: this key space cannot collide with a sibling authority's."""
        self.assertTrue(
            key().canonical_encoding().startswith("mozyo.callback_recovery_admission.v1"),
            "the canonical encoding must be domain-tagged and version-tagged",
        )

    def test_action_kind_separates_two_actions_at_one_anchor(self):
        """Two different recovery kinds at the same anchor are two actions, not one."""
        self.assertNotEqual(
            key(action_kind="callback_sweep_recovery").digest(),
            key(action_kind="worker_exception_replay").digest(),
        )

    def test_blank_field_is_refused(self):
        for field in _BASE:
            with self.subTest(field=field):
                with self.assertRaises(RecoveryKeyError):
                    key(**{field: "  "})

    def test_unknown_schema_version_is_refused(self):
        """An unknown key schema is refused, never reinterpreted under this version's meaning."""
        with self.assertRaises(RecoveryKeyError):
            RecoveryAdmissionKey(**_BASE, schema_version=RECOVERY_KEY_SCHEMA_VERSION + 1)


class RecoveryMarkerWireTests(unittest.TestCase):
    def test_marker_round_trips_to_the_same_key(self):
        """The producer's wire format must reconstruct the exact key the consumer claims on."""
        marker = render_recovery_action_marker(**_MARKER_ARGS)
        lookup = resolve_recovery_action_key(
            [_Entry("80500", f"## record body\n\n{marker}")], recovery_action_journal="80500"
        )
        self.assertTrue(lookup.resolved, lookup.detail)
        self.assertEqual(lookup.key.digest(), key().digest())

    def test_delimiter_bearing_value_is_refused_at_render(self):
        """A value that cannot round-trip is refused at write time, not detected later.

        A ``:`` in a field would forge a marker boundary and read back as a DIFFERENT well-formed
        key — a silent identity forgery. It must be impossible to mint.
        """
        for bad in ("a:b", "a=b", "a]b", "a[b", "a b"):
            with self.subTest(value=bad):
                with self.assertRaises(RecoveryKeyError):
                    render_recovery_action_marker(**{**_MARKER_ARGS, "route_identity": bad})

    def test_journal_id_is_the_owning_entry_never_the_marker(self):
        """The anchor authority is the entry's own id: a marker cannot self-report where it landed."""
        marker = render_recovery_action_marker(**_MARKER_ARGS)
        self.assertNotIn("recovery_action_journal", marker)
        lookup = resolve_recovery_action_key(
            [_Entry("99999", f"body\n\n{marker}")], recovery_action_journal="99999"
        )
        self.assertEqual(lookup.key.recovery_action_journal, "99999")


class RecoveryLookupFailClosedTests(unittest.TestCase):
    """Every ambiguity is a distinct, named refusal — never a bare None the caller can misread."""

    def test_pane_prose_never_yields_a_key(self):
        """Required: the key comes from the structured marker, never from prose (j#80984).

        A note that *describes* a recovery in words — including one quoting the handoff pointer —
        must not produce an admissible key.
        """
        prose = (
            "## Gate: progress_log — callback sweep record\n"
            "recovery for anchor 79990, route claude-worker-1, receiver codex, "
            "action callback_sweep_recovery, workspace ws-1, lane lane_a generation 1\n"
            "[mozyo:handoff:source=redmine:issue=13883:journal=80500:kind=reply:to=codex]"
        )
        lookup = resolve_recovery_action_key(
            [_Entry("80500", prose)], recovery_action_journal="80500"
        )
        self.assertFalse(lookup.resolved)
        self.assertEqual(lookup.reason, LOOKUP_MARKER_ABSENT)

    def test_absent_entry(self):
        lookup = resolve_recovery_action_key([], recovery_action_journal="80500")
        self.assertEqual(lookup.reason, LOOKUP_ENTRY_ABSENT)

    def test_blank_journal(self):
        lookup = resolve_recovery_action_key([], recovery_action_journal="")
        self.assertEqual(lookup.reason, LOOKUP_ENTRY_ABSENT)

    def test_two_entries_claiming_one_id_is_ambiguous(self):
        marker = render_recovery_action_marker(**_MARKER_ARGS)
        lookup = resolve_recovery_action_key(
            [_Entry("80500", marker), _Entry("80500", marker)], recovery_action_journal="80500"
        )
        self.assertEqual(lookup.reason, LOOKUP_ENTRY_AMBIGUOUS)

    def test_two_action_markers_in_one_note_is_ambiguous(self):
        """Two actions on one record: the identity is not guessed."""
        a = render_recovery_action_marker(**_MARKER_ARGS)
        b = render_recovery_action_marker(**{**_MARKER_ARGS, "route_identity": "other-worker"})
        lookup = resolve_recovery_action_key(
            [_Entry("80500", f"{a}\n{b}")], recovery_action_journal="80500"
        )
        self.assertEqual(lookup.reason, LOOKUP_MARKER_AMBIGUOUS)

    def test_marker_missing_a_field_is_malformed(self):
        lookup = resolve_recovery_action_key(
            [
                _Entry(
                    "80500",
                    "[mozyo:workflow-event:kind=callback_recovery_action:schema_version=1:"
                    "anchor=79990:workspace=ws-1:lane=lane_a]",
                )
            ],
            recovery_action_journal="80500",
        )
        self.assertEqual(lookup.reason, LOOKUP_MARKER_MALFORMED)

    def test_marker_without_schema_version_is_malformed(self):
        """An unversioned key cannot be interpreted under this version's meaning."""
        lookup = resolve_recovery_action_key(
            [
                _Entry(
                    "80500",
                    "[mozyo:workflow-event:kind=callback_recovery_action:anchor=79990:"
                    "workspace=ws-1:lane=lane_a:lane_generation=1:route=claude-worker-1:"
                    "receiver=codex:action_kind=callback_sweep_recovery]",
                )
            ],
            recovery_action_journal="80500",
        )
        self.assertEqual(lookup.reason, LOOKUP_MARKER_MALFORMED)

    def test_sweep_record_marker_alone_is_not_an_action(self):
        """A zero-send resolution record carries the sweep marker but no action: nothing to admit."""
        lookup = resolve_recovery_action_key(
            [
                _Entry(
                    "80500",
                    "[mozyo:workflow-event:kind=callback_sweep_record:lane=lane_a:"
                    "lane_generation=1:anchor=79990:outcome=progress_without_callback]",
                )
            ],
            recovery_action_journal="80500",
        )
        self.assertEqual(lookup.reason, LOOKUP_MARKER_ABSENT)


if __name__ == "__main__":
    unittest.main()
