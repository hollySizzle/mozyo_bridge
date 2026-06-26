"""Feature-slug bounded-context root (Redmine #12570 pilot).

US #12570 (parent Feature #12533 `140_ソース配置管理`) reflects the Redmine
Epic/Feature ordering in the source layout via import-safe slug packages:
``features/<epic_slug>/<feature_slug>/``. The Redmine numeric ordering is kept as
mapping metadata in ``vibes/docs/specs/bounded-context-map.md`` (not baked into the
import path, since ``110_...`` is not a valid Python identifier).

Modules relocated here are behavior-preserving moves; their former
``mozyo_bridge.<layer>.<module>`` import paths remain available as ``sys.modules``
compatibility facades, following the #12492/#12493 facade pattern. Facade
retirement is a separately-gated later stage (``fallback-retirement-ledger``), not
done here.

Plan: ``vibes/docs/logics/source-layout-bounded-context-migration.md``.
"""
