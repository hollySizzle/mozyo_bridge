"""`record_matches_binding` owner-binding predicate tests (Redmine #13811).

The single place a process-only lifecycle action (hibernate / quarantine / replace /
retire) decides "is this the lane my request names?" for BOTH owner-binding kinds — an
**issue** lane (owned by ``issue_id``) and a **project-gateway** lane (owned by a canonical
full ``project_scope``, empty issue). These pin the pure verdict directly, so the adapter's
identity seam has committed coverage independent of any one action's wiring:

- the **issue** path is byte-identical to the pre-#13811 hard-coded ``record.issue_id ==
  issue`` check (a project scope of ``""`` never changes an issue-owned lane's verdict);
- the **project-gateway** path matches ONLY a row that is ``binding_kind='project_gateway'``
  AND carries the exact requested scope AND owns no issue — a scope is never confused with
  an issue, and a row of the wrong kind / scope / with a stray issue never matches;
- ``None`` (no row) and every divergent field is a fail-closed non-match.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_binding import record_matches_binding
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    BINDING_KIND_PROJECT_GATEWAY,
    LaneLifecycleRecord,
)

WS = "wsMain"
ISSUE = "13811"
SCOPE = "giken-cloud-drive/project-x"


def _issue_row(issue: str = ISSUE, lane: str = "issue_13811_x") -> LaneLifecycleRecord:
    return LaneLifecycleRecord(
        repo_workspace_id=WS,
        lane_id=lane,
        issue_id=issue,
        binding_kind=BINDING_KIND_ISSUE,
    )


def _gateway_row(
    scope: str = SCOPE, lane: str = "pgwv1_x", issue: str = ""
) -> LaneLifecycleRecord:
    return LaneLifecycleRecord(
        repo_workspace_id=WS,
        lane_id=lane,
        issue_id=issue,
        binding_kind=BINDING_KIND_PROJECT_GATEWAY,
        project_scope=scope,
    )


class RecordMatchesBindingTest(unittest.TestCase):
    # -- None / fail-closed ---------------------------------------------------

    def test_none_row_never_matches_either_kind(self) -> None:
        self.assertFalse(record_matches_binding(None, issue_id=ISSUE))
        self.assertFalse(record_matches_binding(None, project_scope=SCOPE))

    # -- issue binding (byte-identical to the pre-#13811 check) ---------------

    def test_issue_row_matches_its_own_issue(self) -> None:
        self.assertTrue(record_matches_binding(_issue_row(), issue_id=ISSUE))

    def test_issue_row_does_not_match_a_different_issue(self) -> None:
        self.assertFalse(record_matches_binding(_issue_row(), issue_id="99999"))

    def test_issue_verdict_ignores_whitespace_like_the_stored_norm(self) -> None:
        # The stored issue_id is normalized; the incoming id is trimmed the same way, so a
        # padded caller id still matches (the byte-identical `norm` on both sides).
        self.assertTrue(record_matches_binding(_issue_row(), issue_id="  13811 "))

    def test_project_scope_empty_is_the_issue_path_even_for_a_gateway_row(self) -> None:
        # With no project_scope the predicate is on the ISSUE path: a gateway row (empty
        # issue) only matches an (also empty) issue id — never the caller's real issue.
        self.assertFalse(record_matches_binding(_gateway_row(), issue_id=ISSUE))
        self.assertTrue(record_matches_binding(_gateway_row(), issue_id=""))

    # -- project-gateway binding ---------------------------------------------

    def test_gateway_row_matches_its_exact_scope(self) -> None:
        self.assertTrue(record_matches_binding(_gateway_row(), project_scope=SCOPE))

    def test_gateway_scope_match_ignores_the_issue_arg(self) -> None:
        # A project-gateway action still passes the anchor's issue as issue_id; when a scope
        # is supplied it is ignored, so the match is on the scope alone.
        self.assertTrue(
            record_matches_binding(_gateway_row(), issue_id=ISSUE, project_scope=SCOPE)
        )

    def test_gateway_row_does_not_match_a_different_scope(self) -> None:
        self.assertFalse(
            record_matches_binding(_gateway_row(), project_scope="other/scope")
        )

    def test_scope_is_the_full_string_never_a_prefix(self) -> None:
        # The scope is the whole canonical string, matched exactly — a divergent scope is a
        # non-match, never a coerced / prefix one.
        self.assertFalse(
            record_matches_binding(
                _gateway_row(scope="giken-cloud-drive/project-x/sub"),
                project_scope=SCOPE,
            )
        )

    def test_issue_row_never_matches_a_project_scope_request(self) -> None:
        # An issue-kind row can never satisfy a project-gateway request, even if some scope
        # string were present: the kind must be project_gateway.
        self.assertFalse(record_matches_binding(_issue_row(), project_scope=SCOPE))

    def test_gateway_row_carrying_a_stray_issue_is_a_non_match(self) -> None:
        # A well-formed project-gateway lane owns NO issue. A (malformed) gateway-kind row
        # that also carries an issue is refused rather than matched — fail closed on a row
        # that cannot exist through the declaration surface.
        self.assertFalse(
            record_matches_binding(
                _gateway_row(issue=ISSUE), project_scope=SCOPE
            )
        )

    def test_issue_kind_row_with_matching_scope_string_still_needs_the_kind(self) -> None:
        # Defense in depth: even an issue-kind row whose project_scope column happened to
        # equal the request is a non-match, because binding_kind is not project_gateway.
        row = LaneLifecycleRecord(
            repo_workspace_id=WS,
            lane_id="issue_row_with_scope",
            issue_id="",
            binding_kind=BINDING_KIND_ISSUE,
            project_scope=SCOPE,
        )
        self.assertFalse(record_matches_binding(row, project_scope=SCOPE))


if __name__ == "__main__":
    unittest.main()
