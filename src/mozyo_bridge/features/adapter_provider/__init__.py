"""``adapter_provider`` epic-slug package (Redmine Epic #12504, order 140).

Holds the adapter / provider bounded context for the US #12595 source-layout
expansion (parent US #12590, Feature #12533 `140_ソース配置管理`). The context
covers the ticket adapter common contract, the Redmine adapter, the presentation
provider, the plugin manifest / marketplace, and the provider registry
(``vibes/docs/specs/bounded-context-map.md`` `### adapter_provider`).

Epic #12504 has no import-safe Feature-level slug defined yet, so this round uses
the layer-leaf shape ``features/adapter_provider/<layer>/<module>.py`` (R1,
#12591 j#65435): the DDD layer (``domain`` / ``application`` / ``infrastructure``)
is kept on the path so the distinct same-named real modules across layers do not
collide and the legacy ``mozyo_bridge.<layer>.<module>`` import paths can be
preserved as ``sys.modules`` facades. Modules relocated here are
behavior-preserving moves; facade retirement is a separately-gated later stage
(``fallback-retirement-ledger``), not done here. Epic/Feature → slug mapping (with
Redmine numeric ordering metadata) is the ``bounded-context-map.md`` authority.
"""
