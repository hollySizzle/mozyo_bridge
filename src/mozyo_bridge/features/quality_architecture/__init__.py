"""``quality_architecture`` epic-slug package (Redmine Epic #12505, order 150).

Holds the ``150_品質・アーキテクチャ統治`` bounded context — the meta context that
governs the other contexts' test structure, source placement, module health, and
CI / verification policy (``vibes/docs/specs/bounded-context-map.md``).

US #12596 (parent US #12590, Feature #12533 `140_ソース配置管理`) is the
quality_architecture slice of the full Source/Test slug-layout expansion. Per the
#12591 coordination (j#65435 R1 decision), real modules keep the DDD ``layer``
dimension inside the epic-slug package as
``features/quality_architecture/<layer>/<module>.py`` (flat would collide
same-name ``domain/*`` vs ``application/*`` modules across the wider migration).
No Feature-level slug is invented here: only ``execution_platform`` has defined
Feature slugs, so quality_architecture stays Epic-level this round.

Relocated modules are behavior-preserving moves; their former
``mozyo_bridge.<layer>.<module>`` import paths remain available as ``sys.modules``
compatibility facades. Facade retirement is a separately-gated later stage
(``fallback-retirement-ledger``), not done here.
"""
