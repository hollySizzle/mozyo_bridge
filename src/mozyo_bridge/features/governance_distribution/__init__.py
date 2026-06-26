"""``governance_distribution`` epic-slug package (Redmine Epic #12503, order 130).

Holds the governance / scaffold-distribution bounded context as the #12590
Source/Test slug-layout expansion (child task #12594) carries the #12570 pilot
shape to the remaining ``src/**``. Epic/Feature → slug mapping (with Redmine
numeric ordering metadata) is the ``vibes/docs/specs/bounded-context-map.md``
authority; the R1 layer-leaf target shape
``features/<epic_slug>/[<feature_slug>/]<layer>/<module>.py`` is recorded in
Redmine #12591 j#65435.

Modules relocated here are behavior-preserving moves; their former
``mozyo_bridge.application.<module>`` import paths remain available as
``sys.modules`` compatibility facades (the #12492/#12493/#12570 facade idiom).
``scaffold/`` and ``docs_tools/`` are held this round as packaged/fixed surfaces
(j#65435 Decision 3). ``application/release.py`` (the deferred 4th governance
module, allowlisted at 1470 lines) was completed under #12591 integration: the
move and its ``module_health.yaml`` allowlist path re-key land in one atomic
commit (#12591 j#65488).
"""
