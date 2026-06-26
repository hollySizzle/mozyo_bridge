"""``e_150_quality_architecture`` Redmine-numbered Epic package (Epic #12505,
``150_品質・アーキテクチャ統治``).

Holds the ``150_品質・アーキテクチャ統治`` bounded context — the meta context that
governs the other contexts' test structure, source placement, module health, and
CI / verification policy (``vibes/docs/specs/bounded-context-map.md``).

#12628 (parent US #12622, Feature #12533 `140_ソース配置管理`) is the
quality_architecture slice of the Redmine-numbered layout correction. It supersedes
the earlier ``features/quality_architecture/<layer>/`` slug pilot (US #12596): runtime
bodies now live under the Redmine-numbered Epic/Feature package
``e_<order>_<epic_slug>/f_<order>_<feature_slug>/<layer>/<module>.py`` per the
#12623 migration map (``vibes/docs/logics/source-layout-bounded-context-migration.md``,
``## #12622 Redmine-Numbered Layout Correction``). The DDD ``layer`` dimension is kept
inside the Feature package (flat would collide same-name ``domain/*`` vs
``application/*`` modules across the wider migration).

Relocated modules are behavior-preserving moves; their former
``mozyo_bridge.<layer>.<module>`` import paths remain available as ``sys.modules``
compatibility facades. Facade retirement is a separately-gated later stage
(``fallback-retirement-ledger``), not done here.
"""
