"""``operations_cockpit`` epic-slug package (Redmine Epic #12502, order 120).

Holds the operations-cockpit bounded context for the US #12593 source-layout
expansion (parent US #12590, Feature #12533 `140_ソース配置管理`). The context
covers the cockpit read model, the served Web UI, cockpit operations / preflight,
display grouping / layout, and attention / freshness projection
(``vibes/docs/specs/bounded-context-map.md`` `### operations_cockpit`).

Epic #12502 has no import-safe Feature-level slug defined yet, so this round uses
the layer-leaf shape ``features/operations_cockpit/<layer>/<module>.py`` (R1,
#12591 j#65435): the DDD layer (``domain`` / ``application``) is kept on the path
so the distinct same-named real modules across layers do not collide and the
legacy ``mozyo_bridge.<layer>.<module>`` import paths can be preserved as
``sys.modules`` facades. Modules relocated here are behavior-preserving moves;
facade retirement is a separately-gated later stage
(``fallback-retirement-ledger``), not done here. Epic/Feature → slug mapping (with
Redmine numeric ordering metadata) is the ``bounded-context-map.md`` authority.

Held this round (recorded residual, #12591 j#65435 Decision 3): the
``domain/presentation_grouping/`` subpackage (relative imports → needs a package
facade) and ``presentation_state`` (already a ``core/state`` facade from #12493).
"""
