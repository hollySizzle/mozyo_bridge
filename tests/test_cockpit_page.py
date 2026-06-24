"""Served cockpit HTML / static page + browser-smoke tests (Redmine #12323 split).

Focused on :mod:`mozyo_bridge.application.cockpit_page`: the served document's
structure, safety properties (DOM-only rendering, no off-host assets), and
visual-fit / browser-smoke contract (Redmine #12298). Split out of
``test_cockpit_ui`` and ``test_grouped_served_cockpit`` (#12323) so the
page-rendering responsibility is tested on its own, separate from the served-API
payload contract and the action / preflight bridge.

``ServedCockpitSmokeTest`` asserts the served document is a non-blank page whose
core controls, freshness / unavailable state, and unit rows stay visible and
contained at desktop and mobile-ish viewport widths, with no off-host assets and
a stable ``/api/units`` data contract. Those assertions pin the structural
anchors and CSS fit properties the rendering must keep; a real-browser pass is
recorded out of band in the issue journal.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_page import INDEX_HTML_TEMPLATE
from mozyo_bridge.application.otel_receiver import build_server


def pane(pane_id: str, session: str, agent: str, cwd: str = "") -> dict:
    return {
        "id": pane_id,
        "location": f"{session}:1.0",
        "command": agent,
        "cwd": cwd,
        "window_name": agent,
        "pane_active": "1",
    }


def _js_string_list(template: str, const_name: str) -> list[str]:
    """Extract a ``const NAME = ["a", "b"];`` whitelist from the page script.

    Keeps the test's notion of the front-end's state vocabulary in sync with the
    served template instead of hard-coding a copy that can silently drift.
    """
    match = re.search(rf"const {const_name} = \[([^\]]*)\];", template)
    assert match, f"{const_name} whitelist not found in served template"
    return re.findall(r'"([^"]+)"', match.group(1))


class IndexHtmlTemplateTest(unittest.TestCase):
    """Pure assertions on the served template string (no HTTP server needed)."""

    def test_rendering_never_uses_innerhtml(self) -> None:
        # Review #56197 finding 2: payload strings (workspace / session /
        # path names) are local but untrusted input; the page must build
        # DOM via textContent / createElement so HTML metacharacters in
        # them render as text instead of executing. Pin the approach.
        self.assertNotIn("innerHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("outerHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("insertAdjacentHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("document.write", INDEX_HTML_TEMPLATE)
        self.assertIn("textContent", INDEX_HTML_TEMPLATE)
        self.assertIn("createElement", INDEX_HTML_TEMPLATE)

    def test_index_has_reload_button_and_freshness_display(self) -> None:
        # Redmine #12225: the page exposes a manual Reload affordance and a
        # freshness line, rendered via DOM APIs (whitelisted display-state
        # class), never innerHTML.
        self.assertIn('id="reload"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="observation"', INDEX_HTML_TEMPLATE)
        self.assertIn("renderObservation", INDEX_HTML_TEMPLATE)
        self.assertIn("KNOWN_DISPLAY_STATES", INDEX_HTML_TEMPLATE)
        self.assertIn("data.observation", INDEX_HTML_TEMPLATE)
        # The reload button drives an explicit re-fetch.
        self.assertIn(
            "getElementById('reload').addEventListener('click', refresh)",
            INDEX_HTML_TEMPLATE,
        )
        # Still DOM-API only (no HTML injection sink introduced).
        self.assertNotIn("innerHTML", INDEX_HTML_TEMPLATE)


class GroupedRenderingTest(unittest.TestCase):
    """Pin the #12377 grouped scannability rendering (template assertions).

    These pin that the served grouped view makes the project / lane / pane-role
    relationships scannable — a Project Group header, a per-lane row, a fixed
    Codex / Claude role matrix, and clear missing / one-sided / stale state —
    while keeping the DOM-only no-injection property and not touching the
    ``/api/units`` payload contract.
    """

    def test_grouped_role_vocabulary_is_whitelisted(self) -> None:
        # The role matrix renders a fixed slot per canonical role. The class a
        # slot uses must derive from this whitelist + payload presence, never a
        # payload-supplied string, so the (local but untrusted) payload cannot
        # inject a class. Pin the whitelist and that it matches the domain
        # vocabulary (codex, claude), Codex first.
        roles = _js_string_list(INDEX_HTML_TEMPLATE, "GROUPED_ROLES")
        self.assertEqual(roles, ["codex", "claude"])

    def test_grouped_role_matrix_present_and_missing(self) -> None:
        # Acceptance (#12377): same-lane Codex / Claude read as one group via a
        # fixed role matrix on the one lane row, and a one-sided lane shows the
        # absent role as a "missing" slot.
        self.assertIn("function roleSlot(", INDEX_HTML_TEMPLATE)
        self.assertIn("function laneRow(", INDEX_HTML_TEMPLATE)
        self.assertIn("role-matrix", INDEX_HTML_TEMPLATE)
        self.assertIn("role-present", INDEX_HTML_TEMPLATE)
        self.assertIn("role-missing", INDEX_HTML_TEMPLATE)
        # The missing slot carries a visible "missing" label.
        self.assertIn("role-missing-tag", INDEX_HTML_TEMPLATE)
        self.assertRegex(INDEX_HTML_TEMPLATE, r"textContent\s*=\s*'missing'")

    def test_grouped_project_group_separation(self) -> None:
        # Acceptance (#12377): project / lane / role are visually separated — a
        # managed (configured) group and a default / ungrouped bucket carry
        # distinct classes, and the header shows the projection-only summary.
        self.assertIn("function groupSection(", INDEX_HTML_TEMPLATE)
        self.assertIn("'group ' + (g.managed ? 'managed' : 'default')",
                      INDEX_HTML_TEMPLATE)
        self.assertIn("group-summary", INDEX_HTML_TEMPLATE)
        self.assertIn("group-title", INDEX_HTML_TEMPLATE)
        # The lane identity column is distinct from the role matrix.
        self.assertIn("lane-ident", INDEX_HTML_TEMPLATE)
        self.assertIn("lane-id", INDEX_HTML_TEMPLATE)

    def test_grouped_empty_group_and_stale_stay_visible(self) -> None:
        # Acceptance (#12377): a missing / empty lane group stays visible (never
        # dropped) and a stale / reload-required lane reads as needing attention.
        self.assertIn("no lane observed in this group", INDEX_HTML_TEMPLATE)
        self.assertIn("lane-attention", INDEX_HTML_TEMPLATE)

    def test_grouped_rendering_is_dom_only(self) -> None:
        # The new grouped code path must keep the page's no-injection property:
        # DOM construction only, never an HTML-string sink.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write"):
            self.assertNotIn(sink, INDEX_HTML_TEMPLATE, sink)

    def test_grouped_new_classes_are_styled(self) -> None:
        # Every grouped display class the front end tags a node with must have a
        # CSS rule, or it renders unstyled (often invisible / indistinguishable).
        style = INDEX_HTML_TEMPLATE[
            INDEX_HTML_TEMPLATE.index("<style>"):INDEX_HTML_TEMPLATE.index("</style>")
        ]
        for cls in ("group-summary", "lane-row", "lane-ident", "lane-id",
                    "lane-issue", "lane-state", "lane-attention", "role-matrix",
                    "role-slot", "role-present", "role-missing",
                    "role-missing-tag"):
            self.assertRegex(
                style,
                rf"\.{re.escape(cls)}\b[^{{]*\{{",
                f"grouped class .{cls} has no CSS rule (would render unstyled)",
            )
        # The managed / default group accent variants are styled too.
        self.assertRegex(style, r"\.group\.managed\b[^{]*\{")
        self.assertRegex(style, r"\.group\.default\b[^{]*\{")


class EmptyVersusErrorStateTest(unittest.TestCase):
    """Pin the #12378 acceptance: empty state and error state never render the
    same surface.

    An empty cockpit (the daemon responded, nothing observed) and a
    data-unavailable error (the daemon could not be reached) must be distinct in
    both text and styling, and the flat refresh must no longer silently swallow a
    failed ``/api/units`` fetch — the failure that previously made an unreachable
    daemon look identical to an empty cockpit.
    """

    def _style(self) -> str:
        return INDEX_HTML_TEMPLATE[
            INDEX_HTML_TEMPLATE.index("<style>"):INDEX_HTML_TEMPLATE.index(
                "</style>"
            )
        ]

    def test_flat_view_has_dedicated_empty_and_error_machinery(self) -> None:
        # A dedicated state line and a single helper that drives the ok / empty /
        # error modes, so the empty and error paths cannot diverge by accident.
        self.assertIn('id="units-state"', INDEX_HTML_TEMPLATE)
        self.assertIn("function setUnitsState(", INDEX_HTML_TEMPLATE)
        self.assertIn("EMPTY_UNITS_TEXT", INDEX_HTML_TEMPLATE)
        self.assertIn("ERROR_UNITS_TEXT", INDEX_HTML_TEMPLATE)
        # The empty branch is driven by the rendered-row count; the error branch
        # by the catch path.
        self.assertRegex(INDEX_HTML_TEMPLATE, r"setUnitsState\(rendered \? 'ok' : 'empty'")
        self.assertIn("setUnitsState('error'", INDEX_HTML_TEMPLATE)

    def test_empty_and_error_text_differ(self) -> None:
        empty = re.search(r"EMPTY_UNITS_TEXT = '([^']*)'", INDEX_HTML_TEMPLATE)
        error = re.search(r"ERROR_UNITS_TEXT = '([^']*)'", INDEX_HTML_TEMPLATE)
        self.assertTrue(empty and error, "empty/error text constants not found")
        self.assertNotEqual(empty.group(1), error.group(1))
        # The empty text reads as "nothing to show"; the error text as
        # "could not reach the daemon".
        self.assertIn("empty", empty.group(1))
        self.assertRegex(error.group(1), r"unavailable|reach")

    def test_empty_and_error_classes_are_distinctly_styled(self) -> None:
        # If both classes resolved to the same CSS the two states would read the
        # same even with different text. Require a rule for each and that they
        # differ (the error reads fail-closed, the empty reads neutral).
        style = self._style()
        empty_rule = re.search(r"\.state-empty\b[^{]*\{([^}]*)\}", style)
        error_rule = re.search(r"\.state-error\b[^{]*\{([^}]*)\}", style)
        self.assertTrue(empty_rule, ".state-empty has no CSS rule")
        self.assertTrue(error_rule, ".state-error has no CSS rule")
        self.assertNotEqual(
            empty_rule.group(1).strip(),
            error_rule.group(1).strip(),
            "empty and error states must not share identical styling",
        )

    def test_units_fetch_error_is_not_silently_swallowed(self) -> None:
        # Regression: the prior refresh() caught the /api/units failure and
        # rendered nothing, so an unreachable daemon looked identical to an empty
        # cockpit. Pin that the catch path now surfaces the explicit error state.
        self.assertRegex(
            INDEX_HTML_TEMPLATE,
            r"catch \(e\) \{[^}]*setUnitsState\('error'",
        )

    def test_grouped_empty_state_distinct_from_unavailable(self) -> None:
        # Grouped view: zero groups is an empty projection (neutral note), the
        # malformed / failed fetch is the red "unavailable" error.
        self.assertIn("EMPTY_GROUPED_TEXT", INDEX_HTML_TEMPLATE)
        self.assertIn("data.groups.length", INDEX_HTML_TEMPLATE)
        # The grouped empty note uses the neutral empty class; the error path
        # tags the meta with the fail-closed state class.
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"lane-row state-empty"
        )
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"'muted state-error'"
        )

    def test_state_classes_keep_dom_only_no_injection(self) -> None:
        # The new state machinery must keep the page's no-injection property.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write"):
            self.assertNotIn(sink, INDEX_HTML_TEMPLATE, sink)


class FilterDensityGroupingControlsTest(unittest.TestCase):
    """Pin the #12379 acceptance: the served page exposes minimal filter /
    density / grouping controls that narrow the view without changing the payload
    contract, breaking dense readability, or weakening the no-injection property.

    The controls are display-only: they narrow the already-fetched ``/api/units``
    and ``/api/grouped-units`` payloads in the front end (no new endpoint, no
    server state), and every control value is constrained to a whitelist so it
    can never become an injected class.
    """

    def _style(self) -> str:
        return INDEX_HTML_TEMPLATE[
            INDEX_HTML_TEMPLATE.index("<style>"):INDEX_HTML_TEMPLATE.index(
                "</style>"
            )
        ]

    def test_minimal_filter_controls_present(self) -> None:
        # Acceptance: at least project / lane / role minimal scan filters. A
        # free-text input narrows by project / lane / role substring, a role
        # select narrows to a single canonical role, and an attention checkbox
        # narrows to stale / reload-required units.
        self.assertIn('id="view-controls"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="filter-text"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="filter-role"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="filter-attention"', INDEX_HTML_TEMPLATE)
        # The role filter offers the canonical roles plus "all".
        self.assertIn('<option value="codex">codex</option>', INDEX_HTML_TEMPLATE)
        self.assertIn('<option value="claude">claude</option>',
                      INDEX_HTML_TEMPLATE)

    def test_filter_values_are_whitelisted(self) -> None:
        # No control value may become an injected class: each is constrained to a
        # whitelist before it is read into the view state.
        self.assertEqual(
            _js_string_list(INDEX_HTML_TEMPLATE, "ROLE_FILTERS"),
            ["all", "codex", "claude"],
        )
        self.assertEqual(
            _js_string_list(INDEX_HTML_TEMPLATE, "VIEW_MODES"),
            ["both", "flat", "grouped"],
        )
        self.assertEqual(
            _js_string_list(INDEX_HTML_TEMPLATE, "DENSITY_MODES"),
            ["comfortable", "compact"],
        )

    def test_grouped_flat_view_toggle_present(self) -> None:
        # Acceptance: a grouped / flat view switch (or equivalent scan support).
        # The two sections are wrapped so the view select can flip flat / grouped
        # / both by display only.
        self.assertIn('id="filter-view"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="flat-view"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="grouped-view"', INDEX_HTML_TEMPLATE)
        self.assertIn('<option value="both">both</option>', INDEX_HTML_TEMPLATE)
        self.assertIn('<option value="flat">flat</option>', INDEX_HTML_TEMPLATE)
        self.assertIn('<option value="grouped">grouped</option>',
                      INDEX_HTML_TEMPLATE)
        # The toggle flips section visibility, not the payload contract.
        self.assertRegex(
            INDEX_HTML_TEMPLATE,
            r"getElementById\('flat-view'\)\.style\.display",
        )
        self.assertRegex(
            INDEX_HTML_TEMPLATE,
            r"getElementById\('grouped-view'\)\.style\.display",
        )

    def test_density_toggle_present_and_styled(self) -> None:
        # Acceptance: a dense view that keeps major state and actions readable.
        # The density select toggles a single literal `dense` body class, and
        # compact density has CSS rules so it actually tightens spacing.
        self.assertIn('id="filter-density"', INDEX_HTML_TEMPLATE)
        self.assertRegex(
            INDEX_HTML_TEMPLATE,
            r"classList\.toggle\('dense'",
        )
        style = self._style()
        self.assertRegex(style, r"body\.dense\b[^{]*\{")
        # Dense mode tightens lane-row and table-cell spacing.
        self.assertRegex(style, r"body\.dense\s+\.lane-row\b[^{]*\{")
        self.assertRegex(style, r"body\.dense\s+(th|td)\b")

    def test_view_controls_row_wraps_for_small_viewport(self) -> None:
        # Acceptance: small viewport must avoid text overlap. The new controls
        # row wraps (like #controls) so it never overlaps on a narrow screen.
        style = self._style()
        self.assertRegex(style, r"#view-controls\s*\{[^}]*flex-wrap:\s*wrap")

    def test_filters_are_display_only_no_new_endpoint(self) -> None:
        # The controls narrow the already-fetched payloads in the front end. They
        # must not introduce a new fetch path or a server round-trip: only the
        # existing /api/units, /api/grouped-units, /api/transitions, and the
        # token-gated action endpoints may be fetched.
        fetched = set(re.findall(r"fetch\('(/[^']*)'", INDEX_HTML_TEMPLATE))
        allowed = {
            "/api/units",
            "/api/grouped-units",
            "/api/transitions",
            "/api/actions/",
        }
        for url in fetched:
            self.assertTrue(
                any(url.startswith(a) for a in allowed),
                f"unexpected fetch target {url} — filters must be display-only",
            )
        # The filter helper is a pure substring test (no class / markup sink).
        self.assertIn("function matchesText(", INDEX_HTML_TEMPLATE)
        self.assertIn("function filterActive(", INDEX_HTML_TEMPLATE)

    def test_filtered_empty_is_distinct_from_empty_and_error(self) -> None:
        # A filter that hides every row is a filter result, not an empty cockpit
        # or an unavailable error. Pin distinct "no match" text for both views,
        # rendered with the neutral empty state class.
        self.assertIn("FILTERED_UNITS_TEXT", INDEX_HTML_TEMPLATE)
        self.assertIn("FILTERED_GROUPED_TEXT", INDEX_HTML_TEMPLATE)
        filtered = re.search(
            r"FILTERED_UNITS_TEXT = '([^']*)'", INDEX_HTML_TEMPLATE
        )
        empty = re.search(r"EMPTY_UNITS_TEXT = '([^']*)'", INDEX_HTML_TEMPLATE)
        error = re.search(r"ERROR_UNITS_TEXT = '([^']*)'", INDEX_HTML_TEMPLATE)
        self.assertTrue(filtered and empty and error)
        self.assertNotEqual(filtered.group(1), empty.group(1))
        self.assertNotEqual(filtered.group(1), error.group(1))

    def test_controls_rerender_without_refetch(self) -> None:
        # A control change must re-render from the last payload (immediate, no
        # re-fetch) and persist across the 5s poll. Pin the cached payloads and
        # that applyControls re-renders from them.
        self.assertIn("let lastUnitsData", INDEX_HTML_TEMPLATE)
        self.assertIn("let lastGroupedData", INDEX_HTML_TEMPLATE)
        self.assertIn("function applyControls(", INDEX_HTML_TEMPLATE)
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"if \(lastUnitsData\) renderUnits\(lastUnitsData\)"
        )
        # The controls are wired to re-apply on change / input.
        self.assertIn(
            "getElementById('filter-text').addEventListener('input', applyControls)",
            INDEX_HTML_TEMPLATE,
        )

    def test_filter_controls_keep_dom_only_no_injection(self) -> None:
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write"):
            self.assertNotIn(sink, INDEX_HTML_TEMPLATE, sink)


class ActionAffordanceAndFeedbackTest(unittest.TestCase):
    """Pin the #12380 acceptance: cockpit actions read their target, show a reason
    when disabled, persist their result in the UI, and add no destructive operation.

    These pin the four acceptance criteria as template assertions, all DOM-only and
    over the existing token-gated ``/api/actions/*`` endpoints (no new endpoint, no
    new side effect): a readable action target, a displayed disabled reason, a
    persistent success / failure log (no transient ``alert``), and no destructive /
    external operation introduced.
    """

    def _style(self) -> str:
        return INDEX_HTML_TEMPLATE[
            INDEX_HTML_TEMPLATE.index("<style>"):INDEX_HTML_TEMPLATE.index(
                "</style>"
            )
        ]

    def test_action_button_target_is_readable(self) -> None:
        # Acceptance 1: the action target must be readable. A shared factory sets
        # the target on every button's title + aria-label (textContent stays the
        # short label so the dense layout is unchanged), and each view builds a
        # target description from the identity the operator already sees.
        self.assertIn("function actionButton(", INDEX_HTML_TEMPLATE)
        self.assertRegex(INDEX_HTML_TEMPLATE, r"button\.title\s*=")
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"setAttribute\('aria-label'"
        )
        # The enabled-button description names the target after the label.
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"label\s*\+\s*' → '\s*\+\s*targetDesc"
        )
        # The flat row targets agent @ session; a grouped slot targets role · lane.
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"targetDesc\s*=\s*p\.agent_kind\s*\+\s*' @ '"
        )
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"targetDesc\s*=\s*role\s*\+\s*' · '"
        )

    def test_disabled_action_shows_its_reason(self) -> None:
        # Acceptance 2: a disabled action must show *why*, not just look greyed.
        # Each disable path has a reason constant, the disabled branch carries it in
        # the title, and a visible inline action-reason note is rendered as well.
        self.assertIn("STALE_ACTION_REASON", INDEX_HTML_TEMPLATE)
        self.assertIn("RELOAD_ACTION_REASON", INDEX_HTML_TEMPLATE)
        stale = re.search(r"STALE_ACTION_REASON = '([^']*)'", INDEX_HTML_TEMPLATE)
        reload = re.search(r"RELOAD_ACTION_REASON = '([^']*)'", INDEX_HTML_TEMPLATE)
        self.assertTrue(stale and reload, "action-reason constants not found")
        self.assertNotEqual(stale.group(1), reload.group(1))
        # The disabled branch records the reason in the accessible title.
        self.assertRegex(INDEX_HTML_TEMPLATE, r"\(disabled: '\s*\+\s*disabledReason")
        # A visible inline note carries the reason next to the disabled action.
        self.assertIn("function actionReason(", INDEX_HTML_TEMPLATE)
        self.assertIn("action-reason", INDEX_HTML_TEMPLATE)
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"if \(data\.stale\) actions\.appendChild\(actionReason"
        )
        self.assertRegex(
            INDEX_HTML_TEMPLATE, r"if \(disabled\) slot\.appendChild\(actionReason"
        )

    def test_action_result_persists_instead_of_alert(self) -> None:
        # Acceptance 3: the result / failure reason must stay in the UI. A dedicated
        # log region and a recordAction helper persist each outcome (ok / failed +
        # reason); the transient alert() is gone.
        self.assertIn('id="action-feedback"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="action-log"', INDEX_HTML_TEMPLATE)
        self.assertIn("function recordAction(", INDEX_HTML_TEMPLATE)
        # Both action paths (flat act + grouped actGrouped) route both their
        # success and their failure/error branch through the persistent log.
        self.assertEqual(
            INDEX_HTML_TEMPLATE.count("recordAction(kind, targetDesc, res.ok"), 2
        )
        self.assertEqual(
            INDEX_HTML_TEMPLATE.count("recordAction(kind, targetDesc, false"), 2
        )
        # The transient alert is removed (it vanished on dismiss, leaving no record).
        self.assertNotIn("alert(", INDEX_HTML_TEMPLATE)
        # Success and failure read distinctly.
        self.assertRegex(INDEX_HTML_TEMPLATE, r"ok \? 'action-ok' : 'action-failed'")
        self.assertRegex(INDEX_HTML_TEMPLATE, r"ok \? 'ok' : 'failed'")

    def test_no_destructive_or_external_action_added(self) -> None:
        # Acceptance 4: no destructive / external side-effect operation is added.
        # The only action kinds remain jump / reveal (Finder), and every action
        # request still goes to the existing token-gated /api/actions/* endpoints —
        # no new endpoint, no new verb, the custom token header preserved.
        kinds = set(re.findall(r"\['(jump|reveal)', '(?:jump|Finder)'\]",
                               INDEX_HTML_TEMPLATE))
        self.assertEqual(kinds, {"jump", "reveal"})
        fetched = set(re.findall(r"fetch\('(/[^']*)'", INDEX_HTML_TEMPLATE))
        for url in fetched:
            self.assertTrue(
                url.startswith("/api/units")
                or url.startswith("/api/grouped-units")
                or url.startswith("/api/transitions")
                or url.startswith("/api/actions/"),
                f"unexpected fetch target {url} — no new action endpoint allowed",
            )
        # The token header still gates every action request (no relaxation).
        self.assertIn("X-Mozyo-Cockpit-Token", INDEX_HTML_TEMPLATE)

    def test_action_feedback_classes_are_styled(self) -> None:
        # Every class the action affordance / feedback tags a node with must have a
        # CSS rule, or it renders unstyled (often invisible / indistinguishable).
        style = self._style()
        for cls in ("action-ok", "action-failed", "action-disabled",
                    "action-reason"):
            self.assertRegex(
                style,
                rf"\.{re.escape(cls)}\b[^{{]*\{{",
                f"action class .{cls} has no CSS rule (would render unstyled)",
            )
        self.assertRegex(style, r"#action-log\b[^{]*\{")
        self.assertRegex(style, r"#action-feedback\b[^{]*\{")

    def test_action_affordance_keeps_dom_only_no_injection(self) -> None:
        # The new affordance / feedback code path must keep the page's no-injection
        # property: DOM construction only, never an HTML-string sink.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write"):
            self.assertNotIn(sink, INDEX_HTML_TEMPLATE, sink)


class VisualDesignResponsiveTest(unittest.TestCase):
    """Pin the #12381 acceptance: color encodes state meaning (no decorative
    gradient / nested-card chrome), major information stays non-overlapping at
    desktop and narrow viewport, and button / badge / row text never overflows
    incoherently — all while keeping the DOM-only no-injection property.

    These are template assertions on the served page's CSS contract; the
    real-browser desktop / narrow render + screenshots are recorded out of band
    in the issue journal (acceptance criterion 4).
    """

    def _style(self) -> str:
        return INDEX_HTML_TEMPLATE[
            INDEX_HTML_TEMPLATE.index("<style>"):INDEX_HTML_TEMPLATE.index(
                "</style>"
            )
        ]

    def _root_block(self) -> str:
        style = self._style()
        start = style.index(":root")
        # The :root token block is the first declaration block in the style.
        return style[start:style.index("}", start) + 1]

    def test_semantic_color_tokens_defined_in_root(self) -> None:
        # Acceptance 1: color is used for state *meaning*. The palette is defined
        # once as semantic CSS custom properties, so the green / amber / red /
        # blue vocabulary maps to a named state concept instead of ad-hoc hex.
        root = self._root_block()
        for token in ("--c-ok", "--c-warn", "--c-danger", "--c-info",
                      "--c-neutral", "--c-muted"):
            self.assertRegex(
                root, rf"{re.escape(token)}\s*:\s*#[0-9a-fA-F]{{3,6}}",
                f"semantic token {token} not defined in :root",
            )

    def test_state_classes_resolve_color_through_tokens(self) -> None:
        # The state classes must read their color from the semantic tokens, not a
        # raw literal, so "color == state" is structural and auditable. Pin a
        # representative class per state surface (flat state, redmine, observation,
        # grouped freshness, action feedback).
        style = self._style()
        for selector, token in (
            (r"\.active\b", "--c-ok"),
            (r"\.idle\b", "--c-warn"),
            (r"\.rm-available\b", "--c-info"),
            (r"\.obs-unknown\b", "--c-danger"),
            (r"\.fresh-fresh\b", "--c-ok"),
            (r"\.action-failed\b", "--c-danger"),
        ):
            self.assertRegex(
                style,
                rf"{selector}[^}}]*\{{[^}}]*var\({re.escape(token)}\)",
                f"{selector} should resolve its color via var({token})",
            )

    def test_color_literals_are_centralized_in_root(self) -> None:
        # No scattered hex color literals: every 6-digit color literal must live
        # in the :root token block (so the palette has a single source of truth).
        # A leak elsewhere means a state color drifted out of the semantic set.
        style = self._style()
        root = self._root_block()
        outside = style.replace(root, "")
        leaked = re.findall(r"#[0-9a-fA-F]{6}\b", outside)
        self.assertEqual(
            leaked, [],
            f"hex color literals leaked outside :root tokens: {leaked}",
        )
        # And the token block actually carries the palette.
        self.assertGreaterEqual(len(re.findall(r"#[0-9a-fA-F]{6}\b", root)), 8)

    def test_no_decorative_gradient_or_shadow(self) -> None:
        # Acceptance 1: avoid decorative gradient / over-styled chrome. The quiet
        # local indicator uses flat fills and hairline borders only — no gradient,
        # no drop shadow. Check the actual declarations, not the comment prose
        # (the design rationale comments mention "no gradients / shadows").
        declarations = re.sub(r"/\*.*?\*/", "", self._style(), flags=re.DOTALL)
        for decoration in ("gradient", "box-shadow", "text-shadow"):
            self.assertNotIn(
                decoration, declarations,
                f"decorative {decoration} must not be used (#12381 quiet look)",
            )

    def test_narrow_viewport_media_query_tightens_layout(self) -> None:
        # Acceptance 2: major information stays non-overlapping at a narrow
        # viewport. A max-width media query tightens the outer margin / gaps for a
        # phone-ish width (the rows already wrap via flex-wrap; this keeps the
        # wrapped layout legible without horizontal body overflow).
        style = self._style()
        self.assertRegex(style, r"@media\s*\([^)]*max-width:\s*\d+px")
        media = style[style.index("@media"):]
        self.assertRegex(media, r"body\s*\{\s*margin:\s*0\.5rem")

    def test_flex_identity_and_role_containers_contain_overflow(self) -> None:
        # Acceptance 2 / 3: a long lane / issue / project / role string must wrap
        # inside its column, not overflow the row or widen the body. Flex item
        # default min-width:auto blocks shrinking, so the identity / role / title
        # flex children carry min-width:0, and the text leaves carry overflow-wrap.
        style = self._style()
        for selector in (r"\.lane-ident\b", r"\.role-matrix\b",
                         r"\.group-title\b"):
            self.assertRegex(
                style, rf"{selector}[^}}]*\{{[^}}]*min-width:\s*0",
                f"{selector} should set min-width:0 so it can shrink and wrap",
            )
        for selector in (r"\.lane-id\b", r"\.lane-issue\b", r"\.role-name\b"):
            self.assertRegex(
                style, rf"{selector}[^}}]*\{{[^}}]*overflow-wrap:\s*anywhere",
                f"{selector} should wrap long unbreakable strings",
            )

    def test_badge_like_slots_cap_their_width(self) -> None:
        # Acceptance 3: a badge-like role slot must not push the row wider than the
        # viewport when its contents are long; it caps at the available width and
        # wraps internally.
        style = self._style()
        self.assertRegex(style, r"\.role-slot\b[^}]*\{[^}]*max-width:\s*100%")

    def test_visual_design_keeps_dom_only_no_injection(self) -> None:
        # The visual-design refactor must not introduce an HTML-string sink.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write"):
            self.assertNotIn(sink, INDEX_HTML_TEMPLATE, sink)


class ServedCockpitSmokeTest(unittest.TestCase):
    """Page-level browser smoke against the daemon-served cockpit document."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = build_server(host="127.0.0.1", port=0, home=self.home)
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, daemon=True
        ).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as response:
            return response.status, response.read()

    def _served_html(self) -> str:
        status, body = self._get("/")
        self.assertEqual(200, status)
        return body.decode("utf-8")

    # --- self-contained document --------------------------------------------

    def test_index_serves_self_contained_html(self) -> None:
        status, body = self._get("/")
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        self.assertIn("mozyo cockpit", text)
        # Self-contained: no external asset loads (loopback / no-exfil).
        self.assertNotIn("http://", text.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://", text)
        # The per-process action token is embedded for the action header.
        self.assertIn(self.server.cockpit_token, text)

    # --- blank-page guard ---------------------------------------------------

    def test_served_page_is_not_blank(self) -> None:
        # Acceptance: the operator must never get a blank page. A served
        # document that is a complete HTML page with a visible heading and a
        # rendered body is the minimum smoke that the page actually painted.
        html = self._served_html()
        self.assertGreater(len(html), 500, "served page is suspiciously small")
        self.assertIn("<body>", html)
        self.assertIn("</html>", html)
        # A visible heading is the first thing painted before any fetch lands.
        self.assertRegex(html, r"<h2>\s*mozyo cockpit\s*</h2>")

    # --- core controls / state anchors --------------------------------------

    def test_core_controls_and_states_are_present(self) -> None:
        # Acceptance: Unit rows, freshness, and the unavailable state must be
        # representable in the served page. In the current (pre-grouped) served
        # UI these are: the unit table (Unit/Target row), the observation
        # freshness line, and the stale "runtime unavailable" banner, plus the
        # explicit Reload affordance and the recent-transitions list.
        html = self._served_html()
        for anchor in (
            'id="reload"',          # explicit Reload control
            'id="observation"',     # freshness line (observed_at / freshness)
            'id="stale"',           # tmux-runtime-unavailable banner
            'stale-banner',
            'id="units"',           # the unit rows table
            'id="units-state"',     # empty-vs-error state line (#12378)
            'id="transitions"',     # recent state transitions
        ):
            self.assertIn(anchor, html, anchor)
        # Every column the operator reads per unit is present as a header.
        for header in ("state", "agent", "session", "workspace",
                       "redmine", "actions"):
            self.assertIn(f"<th>{header}</th>", html, header)

    # --- mobile-ish fit -----------------------------------------------------

    def test_responsive_viewport_meta_present(self) -> None:
        # Without a responsive viewport meta a phone browser lays the page out
        # on an emulated ~980px desktop canvas and shrinks it, so controls and
        # rows render tiny / clipped. Pin device-width layout for mobile fit.
        html = self._served_html()
        self.assertRegex(
            html,
            r'<meta\s+name="viewport"\s+content="[^"]*width=device-width',
        )

    def test_overflow_containment_css_present(self) -> None:
        # Acceptance: detect text overflow / button-label overflow. These CSS
        # properties are the structural guards that long workspace / session /
        # path strings and the controls row stay inside the viewport instead of
        # forcing horizontal overflow or overlapping neighbours.
        html = self._served_html()
        style = html[html.index("<style>"):html.index("</style>")]
        # The wide unit table scrolls inside its own wrapper, never the body.
        self.assertIn("#units-wrap", html)
        self.assertRegex(style, r"#units-wrap\s*\{[^}]*overflow-x:\s*auto")
        # Long cell strings wrap instead of widening the table past the screen.
        self.assertRegex(style, r"\btd\b[^}]*overflow-wrap:\s*anywhere")
        # Button labels stay on one line (no mid-label wrap) but the controls
        # row itself wraps so the freshness line never overlaps the button.
        self.assertRegex(style, r"button\s*\{[^}]*white-space:\s*nowrap")
        self.assertRegex(style, r"#controls\s*\{[^}]*flex-wrap:\s*wrap")

    def test_every_runtime_state_class_is_styled(self) -> None:
        # A subtler "blank / invisible text" failure: the front end tags each
        # row's state / redmine / observation cell with a whitelisted class. If
        # any whitelisted state lacks a CSS rule it renders as unstyled
        # (often invisible / indistinguishable) text. Require a rule for each.
        html = self._served_html()
        style = html[html.index("<style>"):html.index("</style>")]
        classes: list[str] = []
        classes += _js_string_list(INDEX_HTML_TEMPLATE, "KNOWN_STATES")
        classes += [
            f"rm-{s}"
            for s in _js_string_list(INDEX_HTML_TEMPLATE, "KNOWN_RM_STATES")
        ]
        classes += [
            f"obs-{s}"
            for s in _js_string_list(
                INDEX_HTML_TEMPLATE, "KNOWN_DISPLAY_STATES"
            )
        ]
        for cls in classes:
            self.assertRegex(
                style,
                rf"\.{re.escape(cls)}\s*\{{",
                f"state class .{cls} has no CSS rule (would render unstyled)",
            )

    def test_no_external_assets(self) -> None:
        # Loopback / no-exfiltration posture, and a fit guard: an off-host
        # asset that fails to load can leave the page blank or unstyled. The
        # served document must reference nothing off 127.0.0.1.
        html = self._served_html()
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html.replace("http://127.0.0.1", ""))
        self.assertNotIn("@import", html)
        self.assertNotIn("<link", html)
        # No external script / image sources either.
        self.assertNotRegex(html, r'src\s*=\s*"https?://')

    # --- data contract that keeps the page from rendering blank --------------

    def test_units_payload_feeds_render_without_blanking(self) -> None:
        # The page paints rows from /api/units. Pin that a live snapshot returns
        # exactly the fields the render loop reads, so the table is not silently
        # blank because a field the front end expects went missing.
        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertFalse(payload["stale"])
        self.assertIsInstance(payload["panes"], list)
        self.assertIn("observation", payload)
        self.assertIn("display_state", payload["observation"])
        row = payload["panes"][0]
        for field in ("pane_id", "agent_kind", "session", "activity"):
            self.assertIn(field, row, field)
        self.assertIn("state", row["activity"])
        # workspace may be None but the key the front end reads is present.
        self.assertIn("workspace", row)

    def test_unavailable_freshness_state_is_surfaced(self) -> None:
        # Acceptance: the freshness / unavailable state must be visible, not
        # hidden. When tmux is unreadable the cache snapshot is served stale —
        # the banner trigger (`stale: true`) fires and the observation envelope
        # derives a fail-closed display state (never healthy), so the operator
        # sees "outdated / unavailable" instead of a falsely-current view.
        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            self._get("/api/units")  # seed the cache from a live snapshot
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=None,  # tmux unavailable -> stale cache snapshot
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertTrue(payload["stale"])
        self.assertIn(
            payload["observation"]["display_state"],
            ("reload_required", "unknown"),
        )


if __name__ == "__main__":
    unittest.main()
