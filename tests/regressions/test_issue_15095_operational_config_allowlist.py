"""Redmine #15095 / #15097 — the coordinator-owned operational config carve-out's boundary.

Owner intent (2026-08-07, US #15095): coordinator-owned repo-local operational config must be
changeable without re-taking a ``codex_direct_edit`` approval for every single file, because
that approval traffic stalls bootstrap without buying safety. The danger is the obvious wrong
implementation of that intent — a broad ``.mozyo-bridge/**`` glob, which would also hand over
the distributed rule package, the generator outputs, managed identity/state, DBs and anything
secret-shaped that ever lands in that directory.

So the carve-out is an EXACT-MATCH allowlist of three files. An explicit owner instruction may
authorize a ticketless edit and is preserved by commit trailers; routine coordinator edits keep
the active-issue journal path. Both modes retain diff review, path-specific fail-closed
verification and a commit.

These tests are derivation-based where derivation is possible:

* the allowlist is parsed out of the central preset's own yaml block, and the second declaration
  of the same set (``### パス別編集権限``) is parsed separately and compared — two hand-written
  copies of one list is how a carve-out silently widens;
* the distribution surfaces (packaged presets, skill mirrors, repo-local rule store) are read
  from disk rather than re-listed, so a preset regenerated without the section fails here;
* the catalog assertion drives the real resolver, so losing ``file_convention`` coverage for an
  allowlist path fails as a resolution failure, not as a text diff.

The negative half matters more than the positive half: a test that only proves the three paths
are present would pass just as happily on a preset that had also granted ``.mozyo-bridge/**``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

CENTRAL_SOURCE = (
    ROOT / "src/mozyo_bridge/scaffold/canonical_sources/governed-workflow/bodies/workflow.md"
)

#: The named section that carries the carve-out. Its absence must fail loudly.
SECTION_HEADING = "### Coordinator-Owned Operational Config Direct Edit"

#: The durable journal token the carve-out requires in place of the pre-edit gate.
JOURNAL_TOKEN = "coordinator_operational_config_edit"

#: The owner-approved allowlist (US #15095 「対象（完全一致allowlist）」). This is the ONE place
#: the expectation is written down by hand; every other list in the repo is compared against it.
EXPECTED_ALLOWLIST = (
    ".mozyo-bridge/config.yaml",
    ".mozyo-bridge/project-defaults.yaml",
    ".mozyo-bridge/workflow-role-bindings.json",
)

#: Paths that must NOT be in the allowlist. The legacy compat name and the catalog are the two
#: realistic mistakes: the first because it renders the same defaults, the second because it is
#: already carved out — by the OTHER lane, with different verification.
FORBIDDEN_IN_ALLOWLIST = (
    ".mozyo-bridge/workspace-defaults.yaml",
    ".mozyo-bridge/docs/catalog.yaml",
    ".mozyo-bridge/rules/**",
    ".mozyo-bridge/scaffold.json",
    ".mozyo-bridge/workspace-anchor.json",
    ".mozyo-bridge/workspace.json",
    ".mozyo-bridge/**",
    ".mozyo-bridge/*",
)

#: The packaged governed presets the canonical body renders into.
GOVERNED_PRESETS = ("redmine-governed", "redmine-rails-governed")

#: The distributed skill body plus its two mirrors.
SKILL_SURFACES = (
    "skills/mozyo-bridge-agent/references/workflow.md",
    "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md",
    ".claude/skills/mozyo-bridge-agent/references/workflow.md",
)

_ALLOWLIST_BLOCK_RE = re.compile(
    r"```yaml\ncoordinator_operational_config_allowlist:\n(?P<body>.*?)\n```", re.S
)
_PERMISSION_BLOCK_RE = re.compile(
    r"^coordinator_operational_config:\n  patterns:\n(?P<body>(?:    - \S+\n)+)", re.M
)
_YAML_ITEM_RE = re.compile(r"^\s*- (?P<path>\S+)\s*$", re.M)
_VERIFICATION_BLOCK_RE = re.compile(
    r"```yaml\n(?P<body>\.mozyo-bridge/config\.yaml:\n.*?)\n```", re.S
)

#: The heading must be matched AT LINE START. The same words appear earlier as an inline
#: backticked cross-reference inside `### Codex Direct Edit Gate`, and a plain `.index()` would
#: silently slice from that prose mention instead — yielding a "section" that spans the gate
#: text and makes every marker assertion below answer about the wrong bytes.
_SECTION_START_RE = re.compile(
    rf"^{re.escape(SECTION_HEADING)}$", re.M
)
_NEXT_SECTION_RE = re.compile(r"^### ", re.M)


def _section(text: str) -> str:
    """The carve-out section's own body: its heading through the next ``### `` heading."""
    start = _SECTION_START_RE.search(text)
    assert start is not None, f"{SECTION_HEADING!r} is not present as a heading"
    rest = _NEXT_SECTION_RE.search(text, start.end())
    return text[start.start() : rest.start()] if rest else text[start.start() :]


def _subsection(text: str, starts_with: str) -> str:
    """One ``#### `` subsection of the carve-out section, by heading prefix.

    Marker assertions have to be scoped to the subsection that OWES the marker. Several of
    these tokens (``source_pointer``, ``readback``) legitimately appear twice inside the
    section — once as a requirement, once inside the journal template — so a whole-section
    substring check keeps passing after the requirement itself is deleted.
    """
    section = _section(text)
    start = re.search(rf"^#### {re.escape(starts_with)}.*$", section, re.M)
    assert start is not None, f"lost the '#### {starts_with}' subsection"
    rest = re.compile(r"^#### ", re.M).search(section, start.end())
    return section[start.start() : rest.start()] if rest else section[start.start() :]


def _packaged_preset(preset: str) -> str:
    path = ROOT / "src/mozyo_bridge/scaffold/presets" / preset / "agent-workflow.md"
    assert path.is_file(), f"missing packaged preset: {path}"
    return path.read_text(encoding="utf-8")


def _allowlist_from(text: str) -> tuple[str, ...]:
    """The allowlist as declared by the carve-out section's own yaml block."""
    match = _ALLOWLIST_BLOCK_RE.search(text)
    assert match is not None, "lost the 'coordinator_operational_config_allowlist' yaml block"
    return tuple(_YAML_ITEM_RE.findall(match.group("body")))


def _permission_patterns_from(text: str) -> tuple[str, ...]:
    """The same set as declared a second time in the path-permission table."""
    match = _PERMISSION_BLOCK_RE.search(text)
    assert match is not None, "lost the 'coordinator_operational_config' path-permission block"
    return tuple(_YAML_ITEM_RE.findall(match.group("body")))


def _required_verification(text: str) -> dict[str, tuple[str, ...]]:
    """The MANDATORY per-path commands, parsed out of the verification yaml block only.

    Scoping this to the block matters: every one of these commands is also named in the
    section's surrounding prose, so a section that had lost a command from its mandatory list
    while still mentioning it in a sentence would satisfy a whole-section substring check. That
    is precisely the difference between "the command is required" and "the command is talked
    about", and it is the half worth guarding.
    """
    section = _section(text)
    match = _VERIFICATION_BLOCK_RE.search(section)
    assert match is not None, "lost the path-specific verification yaml block"
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in match.group("body").splitlines():
        key = re.match(r"^(?P<key>\S+):\s*$", line)
        if key:
            current = key.group("key")
            out[current] = []
            continue
        item = re.match(r"^\s+- (?P<cmd>.+?)(?:\s+#.*)?$", line)
        if item and current is not None:
            out[current].append(item.group("cmd").strip())
    return {key: tuple(values) for key, values in out.items()}


class AllowlistIsExactlyTheOwnerApprovedSet(unittest.TestCase):
    """The carve-out's size is the whole safety argument, so it is pinned by equality."""

    def test_central_preset_declares_exactly_the_three_paths(self) -> None:
        found = _allowlist_from(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        # Multiplicity matters: a duplicated entry would make a superset test pass while the
        # list itself was edited.
        self.assertEqual(len(found), len(set(found)), f"duplicate allowlist entries: {found}")
        self.assertEqual(
            set(found),
            set(EXPECTED_ALLOWLIST),
            "the coordinator operational-config allowlist changed; widening it is a guardrail "
            "change that needs owner intent (US #15095), not a preset edit",
        )

    def test_the_two_declarations_of_the_allowlist_agree(self) -> None:
        # `### パス別編集権限` and the carve-out section each write the list out. If they ever
        # disagree, a reader who only consults the permission table gets a different answer
        # than one who reads the section — which is exactly how scope creeps.
        text = CENTRAL_SOURCE.read_text(encoding="utf-8")
        self.assertEqual(
            set(_permission_patterns_from(text)),
            set(_allowlist_from(text)),
            "the path-permission table and the carve-out section declare different allowlists",
        )

    def test_forbidden_paths_are_absent_from_the_allowlist(self) -> None:
        found = set(_allowlist_from(CENTRAL_SOURCE.read_text(encoding="utf-8")))
        for path in FORBIDDEN_IN_ALLOWLIST:
            with self.subTest(path=path):
                self.assertNotIn(path, found)

    def test_both_governed_presets_render_the_same_allowlist(self) -> None:
        for preset in GOVERNED_PRESETS:
            with self.subTest(preset=preset):
                self.assertEqual(
                    set(_allowlist_from(_packaged_preset(preset))),
                    set(EXPECTED_ALLOWLIST),
                )

    def test_the_repo_local_preset_store_is_not_a_hidden_third_authority(self) -> None:
        # This repo's startup contract reads the repo-local store. It must therefore be the exact
        # packaged preset, not merely a non-conflicting older document that lacks the authority.
        store = ROOT / ".mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md"
        self.assertTrue(store.is_file(), f"repo-local preset store absent: {store}")
        self.assertEqual(
            store.read_text(encoding="utf-8"),
            _packaged_preset("redmine-governed"),
            "repo-local startup preset differs from the packaged authority",
        )


class ExactMatchIsStatedAndNotGlobbed(unittest.TestCase):
    """`.mozyo-bridge/**` is the wrong implementation of the same owner intent."""

    def test_the_section_says_exact_match_and_refuses_glob_expansion(self) -> None:
        section = _section(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        for marker in ("完全一致", "glob", "未登録"):
            with self.subTest(marker=marker):
                self.assertIn(marker, section)

    def test_neither_declared_list_contains_a_glob_metacharacter(self) -> None:
        # This is the structural half, and it is the half that actually binds. "Exact match" is
        # a claim about the LIST, so it is checkable on the list itself rather than on prose
        # around it: an entry carrying `*`, `?` or `[` is a pattern, whatever the sentence above
        # it says. A first version of this test asked instead whether a wildcard mention sat on
        # a line containing a refusal word — which a neighbouring clause in the same paragraph
        # satisfied, so a section rewritten to GRANT `.mozyo-bridge/**` still passed.
        text = CENTRAL_SOURCE.read_text(encoding="utf-8")
        for label, entries in (
            ("allowlist block", _allowlist_from(text)),
            ("path-permission block", _permission_patterns_from(text)),
        ):
            for entry in entries:
                with self.subTest(declaration=label, entry=entry):
                    self.assertNotRegex(
                        entry,
                        r"[*?\[]",
                        f"{label} entry {entry!r} is a glob, not an exact path; the carve-out's "
                        f"whole safety argument is that it cannot expand",
                    )

    def test_the_refusal_of_the_directory_glob_is_stated_verbatim(self) -> None:
        # Pinned as a literal rather than by keyword: rewriting this sentence into a grant
        # removes the literal, which fails here. Both rendered presets carry it too, so a
        # regeneration that dropped it does not slip through on the canonical body alone.
        refusal = "`.mozyo-bridge/**` のような glob へ展開しない"
        for label, text in [("canonical", CENTRAL_SOURCE.read_text(encoding="utf-8"))] + [
            (preset, _packaged_preset(preset)) for preset in GOVERNED_PRESETS
        ]:
            with self.subTest(surface=label):
                self.assertIn(refusal, _section(text))

    def test_the_excluded_families_are_named_not_left_to_inference(self) -> None:
        # A reader deciding about a file NOT in the allowlist needs the exclusions spelled out;
        # "it isn't listed" is a weaker signal than "it is named as excluded".
        section = _section(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        for marker in (
            ".mozyo-bridge/rules/**",
            ".mozyo-bridge/workspace-anchor.json",
            ".mozyo-bridge/workspace-defaults.yaml",  # the legacy compat name
            "generated",
            "DB",
            "secret",
            "未登録 file",
            "既定は deny",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, section)


class PathSpecificVerificationSurvivesTheCarveOut(unittest.TestCase):
    """What is dropped is pre-approval. Verification is what replaces it, so it must be named."""

    #: Each allowlist path and the fail-closed command the section must require for it.
    REQUIRED_COMMANDS = {
        ".mozyo-bridge/config.yaml": "mozyo-bridge config check-parse",
        ".mozyo-bridge/project-defaults.yaml": "mozyo-bridge workspace-defaults --check",
        ".mozyo-bridge/workflow-role-bindings.json": "mozyo-bridge workflow role-authority --json",
    }

    def test_every_allowlist_path_carries_its_own_verification_command(self) -> None:
        for source in (CENTRAL_SOURCE.read_text(encoding="utf-8"),) + tuple(
            _packaged_preset(preset) for preset in GOVERNED_PRESETS
        ):
            required = _required_verification(source)
            # Every allowlist path must appear as its own key: a shared "common" list would let
            # one path's verification stand in for another's.
            self.assertEqual(
                set(EXPECTED_ALLOWLIST) - set(required),
                set(),
                f"allowlist paths without their own verification entry: "
                f"{sorted(set(EXPECTED_ALLOWLIST) - set(required))}",
            )
            for path, command in self.REQUIRED_COMMANDS.items():
                with self.subTest(path=path):
                    self.assertTrue(
                        any(cmd.startswith(command) for cmd in required[path]),
                        f"{path} no longer REQUIRES {command!r}; its mandatory commands are "
                        f"{required[path]}",
                    )

    def test_git_diff_check_is_required_for_every_edit(self) -> None:
        required = _required_verification(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        common = required.get("共通", ())
        self.assertTrue(
            any(cmd.startswith("git diff --check") for cmd in common),
            f"the shared verification entry lost `git diff --check`: {common}",
        )

    def test_the_retained_obligations_are_stated_not_implied(self) -> None:
        section = _section(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        for marker in (
            "差分確認",
            "verification_failed",
            JOURNAL_TOKEN,
            "commit_hash",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, section)

    def test_explicit_owner_mode_is_ticketless_but_commit_anchored(self) -> None:
        authority = _subsection(CENTRAL_SOURCE.read_text(encoding="utf-8"), "Authority mode")
        for marker in (
            "owner_explicit_direct_edit",
            "active_issue: 不要",
            "Owner-Authorized-Direct-Edit: true",
            "Owner-Authorized-Path: <exact-path>",
            "direct edit を明示",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, authority)

    def test_generic_imperatives_do_not_become_owner_direct_edit_authority(self) -> None:
        authority = _subsection(CENTRAL_SOURCE.read_text(encoding="utf-8"), "Authority mode")
        for marker in ("いいからやれ", "一般的な実行要求だけでは", "一意に解決"):
            with self.subTest(marker=marker):
                self.assertIn(marker, authority)

    def test_routine_mode_keeps_the_issue_and_journal(self) -> None:
        authority = _subsection(CENTRAL_SOURCE.read_text(encoding="utf-8"), "Authority mode")
        self.assertIn("coordinator_routine_edit", authority)
        self.assertIn("active issue", authority)
        self.assertIn(JOURNAL_TOKEN, authority)

    def test_role_bindings_carry_the_extra_routing_authority_conditions(self) -> None:
        # Scoped to the requirements subsection, NOT the whole section: `source_pointer` and
        # `readback` also occur in the journal template's `role_binding_conditions` line, so a
        # section-wide check stayed green after the requirement bullet itself was deleted.
        conditions = _subsection(CENTRAL_SOURCE.read_text(encoding="utf-8"), "役割設定")
        for marker in (
            "source_pointer",
            "closed schema",
            "readback",
            "遡及適用しない",
            "再起動境界",
            "active issue",
            "ticketless にしない",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, conditions)

    def test_the_journal_template_records_which_conditions_were_met(self) -> None:
        journal = _subsection(
            CENTRAL_SOURCE.read_text(encoding="utf-8"), f"`{JOURNAL_TOKEN}` Journal"
        )
        for field in (
            "allowlist_paths",
            "intent",
            "diff_confirmed",
            "verification",
            "commit_hash",
            "role_binding_conditions",
        ):
            with self.subTest(field=field):
                self.assertIn(field, journal)

    def test_the_carve_out_does_not_grant_a_review_exemption(self) -> None:
        # `codex_direct_edit` carries a `follow_up_review: false` exemption. This carve-out is
        # an EDIT-authority carve-out only; folding the two together would silently drop review
        # on a surface nobody agreed to exempt.
        section = _section(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        self.assertIn("review exemption ではない", section)

    def test_widening_the_allowlist_is_itself_a_guardrail_change(self) -> None:
        section = _section(CENTRAL_SOURCE.read_text(encoding="utf-8"))
        self.assertIn("本 allowlist 自体の変更", section)


class TheTwoCarveOutsAreNotConflatable(unittest.TestCase):
    """Both drop the pre-edit gate; they require different journals and different verification."""

    def test_the_autonomous_lane_excludes_the_operational_config_paths(self) -> None:
        for preset in GOVERNED_PRESETS:
            text = _packaged_preset(preset)
            start = text.index("#### 既定 path 集合")
            end = text.index("#### `codex_autonomous_edit` Journal", start)
            lane_section = text[start:end]
            with self.subTest(preset=preset):
                for path in EXPECTED_ALLOWLIST:
                    self.assertIn(path, lane_section)
                self.assertIn(JOURNAL_TOKEN, lane_section)

    def test_the_repo_local_lane_doc_points_at_the_other_carve_out(self) -> None:
        body = (
            ROOT / "vibes/docs/rules/codex-autonomous-guardrail-lane.md"
        ).read_text(encoding="utf-8")
        for marker in (SECTION_HEADING.removeprefix("### "), JOURNAL_TOKEN, "#15095"):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)


class AuthorityFollowsTheCoordinatorRole(unittest.TestCase):
    """Finding rolegate: provider binding must not decide operational authority."""

    def test_permission_table_is_role_based_not_codex_keyed(self) -> None:
        text = CENTRAL_SOURCE.read_text(encoding="utf-8")
        start = text.index("coordinator_operational_config:")
        end = text.index("generated物:", start)
        block = text[start:end]
        self.assertIn("resolved coordinator role", block)
        self.assertIn("編集条件:", block)
        self.assertNotIn("codex編集条件:", block)

    def test_executable_contract_gates_before_the_provider_specific_branch(self) -> None:
        text = CENTRAL_SOURCE.read_text(encoding="utf-8")
        start = text.index("@startuml mozyo_bridge_agent_gate_contract")
        end = text.index("@enduml", start)
        contract = text[start:end]
        role_branch = (
            "if ($agent役割がcoordinator() && "
            "$対象がoperational_config_allowlist完全一致()) then (yes)"
        )
        self.assertIn(role_branch, contract)
        self.assertLess(contract.index(role_branch), contract.index("if ($agentがcodex())"))
        self.assertIn("$authority_modeを設定(\"owner_explicit_direct_edit\")", contract)
        self.assertIn("$authority_modeを設定(\"coordinator_routine_edit\")", contract)


class RepoLocalRulesInstallKeepsManifestInSync(unittest.TestCase):
    """Finding presetstore: install updates only the preset identity fields."""

    def test_repo_local_install_updates_only_version_and_hash(self) -> None:
        from mozyo_bridge.scaffold.rules import install_rules, resolve_rules_store

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".mozyo-bridge"
            state_dir.mkdir()
            manifest_path = state_dir / "scaffold.json"
            original = {
                "schema_version": 2,
                "mode": "repo-local",
                "preset": "redmine-governed",
                "preset_version": "stale-version",
                "preset_hash": "stale-hash",
                "generated_by": "sentinel",
                "rule_path": ".mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md",
                "files": {"AGENTS.md": {"sha256": "sentinel-file-hash"}},
                "sentinel": {"preserve": True},
            }
            manifest_path.write_text(
                json.dumps(original, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            written = install_rules(store=resolve_rules_store(repo_local=repo))

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            installed = (
                state_dir
                / "rules/presets/redmine-governed/agent-workflow.md"
            )
            self.assertIn(manifest_path, written)
            self.assertEqual(
                hashlib.sha256(installed.read_bytes()).hexdigest(),
                updated["preset_hash"],
            )
            packaged_version = (
                ROOT
                / "src/mozyo_bridge/scaffold/presets/redmine-governed/VERSION"
            ).read_text(encoding="utf-8").strip()
            self.assertEqual(packaged_version, updated["preset_version"])
            for key in set(original) - {"preset_version", "preset_hash"}:
                with self.subTest(key=key):
                    self.assertEqual(original[key], updated[key])

    def test_malformed_manifest_fails_before_installing_presets(self) -> None:
        from mozyo_bridge.scaffold.rules import install_rules, resolve_rules_store

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".mozyo-bridge"
            state_dir.mkdir()
            (state_dir / "scaffold.json").write_text("{", encoding="utf-8")

            with self.assertRaises(SystemExit):
                install_rules(store=resolve_rules_store(repo_local=repo))

            self.assertFalse((state_dir / "rules").exists())

    def test_non_repo_local_manifest_mode_fails_before_installing_presets(self) -> None:
        from mozyo_bridge.scaffold.rules import install_rules, resolve_rules_store

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".mozyo-bridge"
            state_dir.mkdir()
            (state_dir / "scaffold.json").write_text(
                json.dumps({"mode": "central", "preset": "redmine-governed"}),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                install_rules(store=resolve_rules_store(repo_local=repo))

            self.assertFalse((state_dir / "rules").exists())

class TheCarveOutIsDistributedAndAdopted(unittest.TestCase):
    """Preset, skill body, both mirrors, routers, and the repo-local adoption record."""

    def test_both_governed_presets_ship_the_section(self) -> None:
        for preset in GOVERNED_PRESETS:
            with self.subTest(preset=preset):
                self.assertIn(SECTION_HEADING, _packaged_preset(preset))

    def test_governed_preset_versions_were_bumped_past_the_pre_change_label(self) -> None:
        # A workflow/guardrail change consumers must re-install. The manifest's preset_hash is
        # what forces that; this pins the label so a forgotten bump fails loudly.
        for preset in GOVERNED_PRESETS:
            version = (
                ROOT / "src/mozyo_bridge/scaffold/presets" / preset / "VERSION"
            ).read_text(encoding="utf-8").strip()
            with self.subTest(preset=preset):
                self.assertNotEqual("2026.08.04.1", version)

    def test_the_skill_body_and_both_mirrors_carry_the_pointer(self) -> None:
        for surface in SKILL_SURFACES:
            body = (ROOT / surface).read_text(encoding="utf-8")
            with self.subTest(surface=surface):
                self.assertIn("Coordinator-Owned Operational Config Direct Edit", body)
                self.assertIn(JOURNAL_TOKEN, body)
                for path in EXPECTED_ALLOWLIST:
                    self.assertIn(path, body)

    def test_the_routers_name_the_carve_out_and_its_limits(self) -> None:
        # A router-only reader must not conclude that `.mozyo-bridge/` is free to edit.
        for router in ("AGENTS.md", "CLAUDE.md"):
            body = (ROOT / router).read_text(encoding="utf-8")
            with self.subTest(router=router):
                self.assertIn(JOURNAL_TOKEN, body)
                self.assertIn("完全一致 allowlist", body)
                for path in EXPECTED_ALLOWLIST:
                    self.assertIn(path, body)

    def test_the_repo_local_adoption_record_names_the_owner_anchor(self) -> None:
        body = (ROOT / "vibes/docs/rules/agent-workflow.md").read_text(encoding="utf-8")
        self.assertIn("Coordinator-Owned Operational Config Direct Edit", body)
        self.assertIn("#15095", body)
        for path in EXPECTED_ALLOWLIST:
            with self.subTest(path=path):
                self.assertIn(path, body)


class EveryAllowlistPathResolvesToTheAdoptionRule(unittest.TestCase):
    """Catalog coverage: touching an allowlist file must surface the rule that governs it."""

    def test_docs_resolve_surfaces_the_project_agent_workflow_rule(self) -> None:
        try:
            from mozyo_bridge.docs_tools import CatalogContext, resolve_paths
        except ImportError as exc:  # pragma: no cover - tooling absent
            self.skipTest(f"docs_tools not importable: {exc}")
        context = CatalogContext.build(str(ROOT), None)
        for path in EXPECTED_ALLOWLIST:
            results = resolve_paths(context, [path])
            ids = {
                doc["id"] for entry in results for doc in entry.get("documents", [])
            }
            with self.subTest(path=path):
                self.assertIn(
                    "rule-project-agent-workflow",
                    ids,
                    f"`mozyo-bridge docs resolve {path}` lost the adoption rule; catalog "
                    f"file_convention coverage regressed",
                )


if __name__ == "__main__":
    unittest.main()
