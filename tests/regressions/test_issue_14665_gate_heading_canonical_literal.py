"""Redmine #14665 — ONE canonical gate-heading literal across every governed instruction surface.

The defect (#14664 j#92503): the central preset's ``## Journal Templates`` mandated
``## Gate: review_request`` while the distributed skill's ``implementation_worker`` role
profile mandated ``## Gate: Review Request``. ``workflow glance`` folded both, so nothing was
broken at runtime — but a worker could not comply with one documented source without deviating
from the other, and the two sides were free to drift further apart.

The ruling (central preset ``## Journal Templates`` / ``### Gate Heading Canonical Literal``,
which the dispatch j#92513 fixed as the higher authority): a producer writes
``## Gate: <gate>`` with the Gate Schema's own lower snake_case token, so a heading and its
structured marker (``gate=<gate>``) name the same token the same way. The reading side keeps
folding the space-opened / case-folded spelling so journals written before the ruling stay
readable — an alias contract for READING, not a second spelling anyone may write.

These tests are derivation-based on purpose. The literals are never re-listed here:

* the canonical token universe is parsed out of the *central preset itself* (its Gate Schema
  keys plus its Journal Template headings), so adding a gate to the authority doc widens the
  check automatically;
* the producer-instruction surfaces are derived from ``git ls-files``, so a new preset copy,
  a new skill mirror or a new packaged template joins the scan without an edit here;
* the reading-side aliases are derived from the grammar's canonical token map, so a one-sided
  alias cannot be re-introduced.

A hand-maintained list on either side is exactly what produced this issue.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import (
    role_profile as rp,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (
    CANONICAL_GATE_TOKENS,
    CANONICAL_REVIEW_HEADING,
    canonical_gate_heading,
    fold_issue_gate_facts,
)

#: The central preset's canonical body — the authority this issue resolved the conflict toward.
CENTRAL_SOURCE = (
    ROOT
    / "src/mozyo_bridge/scaffold/canonical_sources/governed-workflow/bodies/workflow.md"
)

#: The named section that carries the ruling. Its absence must fail loudly, not silently pass.
RULING_HEADING = "### Gate Heading Canonical Literal"

_GATE_SCHEMA_RE = re.compile(r"### Gate Schema\n\n```yaml\n(?P<body>.*?)\n```", re.S)
_JOURNAL_TEMPLATES_RE = re.compile(
    r"## Journal Templates\n.*?```markdown\n(?P<body>.*?)\n```", re.S
)
_YAML_KEY_RE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*):", re.M)
_TEMPLATE_HEADING_RE = re.compile(r"^## Gate: (?P<token>\S+)", re.M)

#: A gate heading occurrence anywhere in a document (prefixed shape). Backticked inline spans
#: are excluded from the title so a prose mention cannot masquerade as a heading.
_HEADING_RE = re.compile(r"#{2,}\s*Gate\s*[:：]\s*(?P<title>[^\n`]+)")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_BOUNDED_QUALIFIER_RE = re.compile(r"\s+[—–]\s+")
_WS_RE = re.compile(r"\s+")

#: The role profile template bodies that mandate a durable gate heading, and which token each
#: mandates. Derived checks below turn each entry into the canonical literal.
_ROLE_MANDATED_TOKENS = {
    "implementation_gateway": ("review",),
    "implementation_worker": ("implementation_done", "review_request"),
}


def _canonical_token_universe() -> frozenset[str]:
    """Every gate token the CENTRAL PRESET itself defines (Gate Schema + Journal Templates)."""
    text = CENTRAL_SOURCE.read_text(encoding="utf-8")
    schema = _GATE_SCHEMA_RE.search(text)
    templates = _JOURNAL_TEMPLATES_RE.search(text)
    assert schema is not None, f"{CENTRAL_SOURCE} lost its '### Gate Schema' yaml block"
    assert templates is not None, f"{CENTRAL_SOURCE} lost its '## Journal Templates' block"
    return frozenset(
        _YAML_KEY_RE.findall(schema.group("body"))
        + _TEMPLATE_HEADING_RE.findall(templates.group("body"))
    )


def _tracked_files() -> tuple[str, ...]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return tuple(line for line in out.splitlines() if line)


def _is_producer_surface(path: str) -> bool:
    """A file that TELLS an agent what to write (as opposed to one that reads what was written).

    Reader-side modules (the glance grammar) legitimately quote the non-canonical spellings
    they must keep folding, so they are not instruction surfaces and are not scanned here.
    """
    if path.startswith("tests/"):
        return False
    base = posixpath.basename(path)
    return (
        base in {"agent-workflow.md", "role_profile_templates.yaml"}
        or path.endswith("references/workflow.md")
        or path.endswith("canonical_sources/governed-workflow/bodies/workflow.md")
    )


def _producer_surfaces() -> tuple[str, ...]:
    return tuple(p for p in _tracked_files() if _is_producer_surface(p))


def _normalized_title(raw: str) -> str:
    """The heading title with the qualifiers the grammar strips, but the CASE preserved."""
    stripped = _TRAILING_PAREN_RE.sub("", raw.strip())
    stripped = _BOUNDED_QUALIFIER_RE.split(stripped)[0].strip()
    return _WS_RE.sub(" ", stripped)


class CentralPresetCarriesTheRuling(unittest.TestCase):
    """The authority names the rule; the checks below are only meaningful because it does."""

    def test_ruling_section_exists_in_the_central_source_and_every_generated_preset(
        self,
    ) -> None:
        # The generated / installed governed presets must carry the ruling too — a worker that
        # only ever reads its repo-local preset store must find it there.
        carriers = [
            p
            for p in _producer_surfaces()
            if p.endswith("bodies/workflow.md") or "governed/agent-workflow.md" in p
        ]
        self.assertGreaterEqual(
            len(carriers), 5, f"expected the canonical body + governed presets, got {carriers}"
        )
        for path in carriers:
            with self.subTest(path=path):
                self.assertIn(RULING_HEADING, (ROOT / path).read_text(encoding="utf-8"))


class CanonicalTokenUniverseIsSnakeCase(unittest.TestCase):
    """The authority's own vocabulary is uniformly lower snake_case — the rule is derivable."""

    def test_every_central_preset_gate_token_is_lower_snake_case(self) -> None:
        universe = _canonical_token_universe()
        self.assertGreaterEqual(len(universe), 8, f"universe collapsed to {sorted(universe)}")
        for token in sorted(universe):
            with self.subTest(token=token):
                self.assertRegex(token, r"^[a-z][a-z0-9_]*$")

    def test_grammar_canonical_tokens_are_a_subset_of_the_authority_vocabulary(self) -> None:
        # The runtime may recognize fewer gates than the doc defines (``review_finding_verdict``
        # is deliberately not a lifecycle gate), but it must never canonicalize a token the
        # authority does not define — that would be a third vocabulary.
        #
        # ``blocked`` is exactly such a token TODAY and is pinned as a KNOWN GAP rather than
        # hidden: the grammar folds ``## Gate: blocked`` to a lifecycle gate and lanes durably
        # record parked-state journals, but the central preset's ``### Gate Schema`` defines no
        # ``blocked`` gate (its parked-state field shape lives in the skill's
        # ``## Sublane 完了 guardrail``). Defining that gate is a policy decision this issue
        # does not own, so the gap is pinned to its exact size: it cannot grow silently, and
        # closing it (adding ``blocked`` to the Gate Schema) fails here as a prompt to update
        # this expectation.
        universe = _canonical_token_universe()
        undefined = set(CANONICAL_GATE_TOKENS) - set(universe)
        self.assertEqual(
            undefined,
            {"blocked"},
            "the set of grammar-canonical gate tokens the central preset does not define "
            f"changed: {sorted(undefined)}",
        )


class NoProducerSurfaceWritesANonCanonicalSpelling(unittest.TestCase):
    """The defect itself: two instruction surfaces mandating two spellings of one token."""

    def test_the_surface_scan_is_not_vacuous(self) -> None:
        # A derivation that silently selects nothing would make the scan below pass by
        # accident. Pin that the known instruction families are all present.
        surfaces = _producer_surfaces()
        self.assertGreaterEqual(len(surfaces), 10, f"surface derivation collapsed: {surfaces}")
        for needle in (
            "src/mozyo_bridge/scaffold/canonical_sources/governed-workflow/bodies/workflow.md",
            "src/mozyo_bridge/scaffold/presets/redmine-governed/agent-workflow.md",
            ".mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md",
            "skills/mozyo-bridge-agent/references/workflow.md",
            "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md",
            ".claude/skills/mozyo-bridge-agent/references/workflow.md",
            "src/mozyo_bridge/e_110_execution_platform/f_130_handoff_routing/domain/"
            "role_profile_templates.yaml",
        ):
            with self.subTest(surface=needle):
                self.assertIn(needle, surfaces)

    def test_every_gate_heading_on_every_producer_surface_uses_the_canonical_spelling(
        self,
    ) -> None:
        universe = _canonical_token_universe()
        spaced = {token.replace("_", " "): token for token in universe}
        offenders: list[str] = []
        scanned = 0
        for path in _producer_surfaces():
            text = (ROOT / path).read_text(encoding="utf-8")
            for match in _HEADING_RE.finditer(text):
                title = _normalized_title(match.group("title"))
                if title in universe:
                    scanned += 1
                    continue
                folded = title.lower()
                if folded in universe or folded in spaced:
                    # It names a real gate, but not with the canonical token spelling.
                    offenders.append(f"{path}: '## Gate: {title}'")
        self.assertEqual(
            offenders,
            [],
            "producer surfaces must write the canonical lower snake_case gate token "
            f"(central preset '{RULING_HEADING}'); found: {offenders}",
        )
        self.assertGreater(scanned, 0, "no canonical gate heading was seen at all")


class RoleProfileTemplatesMandateTheDerivedLiteral(unittest.TestCase):
    """The runtime instruction an agent actually receives carries the canonical literal."""

    def test_each_mandating_role_names_the_canonical_literal(self) -> None:
        checked: set[tuple[str, str]] = set()
        for role, tokens in _ROLE_MANDATED_TOKENS.items():
            body = rp.ROLE_PROFILE_TEMPLATES[role]
            for token in tokens:
                with self.subTest(role=role, token=token):
                    self.assertIn(canonical_gate_heading(token), body)
                    checked.add((role, token))
        self.assertEqual(
            checked,
            {(role, token) for role, tokens in _ROLE_MANDATED_TOKENS.items() for token in tokens},
        )

    def test_no_role_profile_template_carries_a_non_canonical_gate_heading(self) -> None:
        universe = _canonical_token_universe()
        spaced = {token.replace("_", " ") for token in universe}
        offenders = []
        for role, body in sorted(rp.ROLE_PROFILE_TEMPLATES.items()):
            for match in _HEADING_RE.finditer(body):
                title = _normalized_title(match.group("title"))
                if title in universe:
                    continue
                if title.lower() in universe or title.lower() in spaced:
                    offenders.append(f"{role}: '## Gate: {title}'")
        self.assertEqual(offenders, [])

    def test_canonical_review_heading_is_derived_not_re_spelled(self) -> None:
        self.assertEqual(CANONICAL_REVIEW_HEADING, canonical_gate_heading("review"))


class SkillSectionAndPackagedTemplatesStayByteIdentical(unittest.TestCase):
    """The sync that was prose-only until now (spec: bump ``version``, change both together).

    #14665 is a drift between two copies of one contract. The packaged
    ``role_profile_templates.yaml`` and the distributed skill's
    ``### 固定 role profile template`` fenced blocks are exactly such a pair, and nothing
    executed pinned them together — so this pins them.
    """

    _ROLE_BLOCK_RE = re.compile(
        r"^```text\n(?P<body># role profile: (?P<role>\w+)\n.*?)^```$", re.M | re.S
    )

    def _skill_blocks(self, path: str) -> dict[str, str]:
        text = (ROOT / path).read_text(encoding="utf-8")
        return {
            m.group("role"): m.group("body").rstrip("\n")
            for m in self._ROLE_BLOCK_RE.finditer(text)
        }

    def test_every_skill_copy_reproduces_the_packaged_template_bodies(self) -> None:
        copies = [p for p in _producer_surfaces() if p.endswith("references/workflow.md")]
        self.assertGreaterEqual(len(copies), 3, f"skill copy derivation collapsed: {copies}")
        expected = {role: body.rstrip("\n") for role, body in rp.ROLE_PROFILE_TEMPLATES.items()}
        for path in copies:
            with self.subTest(path=path):
                self.assertEqual(self._skill_blocks(path), expected)


class ReadingSideAliasContractIsPreserved(unittest.TestCase):
    """Acceptance: the ruling must not make historical journals unreadable."""

    def test_canonical_and_space_opened_spellings_fold_to_the_same_gate(self) -> None:
        # Derived over the WHOLE canonical token map; the materialized checks are compared to
        # the derived set so a shrunken loop cannot pass as full coverage.
        expected = {
            (token, variant)
            for token in CANONICAL_GATE_TOKENS
            for variant in (token, token.replace("_", " "), token.replace("_", " ").title())
        }
        checked: set[tuple[str, str]] = set()
        for token, gate in sorted(CANONICAL_GATE_TOKENS.items()):
            for variant in (token, token.replace("_", " "), token.replace("_", " ").title()):
                with self.subTest(token=token, variant=variant):
                    facts = fold_issue_gate_facts([("100", f"## Gate: {variant}\n- x")])
                    self.assertIsNotNone(facts, f"'## Gate: {variant}' stopped folding")
                    self.assertEqual(facts.latest_gate, gate)
                    checked.add((token, variant))
        self.assertEqual(checked, expected)

    def test_the_pre_ruling_documented_literals_still_fold(self) -> None:
        # The exact three literals the skill mandated before this issue. They must keep
        # reading forever — every durable journal recorded under the old wording used them.
        for heading, token in (
            ("## Gate: Implementation Done", "implementation_done"),
            ("## Gate: Review Request", "review_request"),
            ("## Gate: Review", "review"),
        ):
            with self.subTest(heading=heading):
                facts = fold_issue_gate_facts([("100", f"{heading}\n- x")])
                self.assertIsNotNone(facts)
                self.assertEqual(facts.latest_gate, CANONICAL_GATE_TOKENS[token])

    def test_every_recognized_spelling_has_its_underscore_space_twin(self) -> None:
        # The twin lists were the drift source; assert the derivation closed over them, so a
        # future edit cannot add a one-sided alias that reads under one spelling only.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
            glance_journal_grammar as grammar,
        )

        mapping = grammar._HEADING_GATE
        for spelling, gate in sorted(mapping.items()):
            twin = spelling.replace("_", " ")
            with self.subTest(spelling=spelling):
                self.assertIn(twin, mapping)
                self.assertEqual(mapping[twin], gate)


if __name__ == "__main__":
    unittest.main()
