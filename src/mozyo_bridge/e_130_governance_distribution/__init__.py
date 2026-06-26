"""``e_130_governance_distribution`` Epic package (Redmine Epic #12503, order 130).

Holds the governance / scaffold-distribution bounded context under the #12622
Redmine-numbered Epic/Feature layout correction (child task #12626). The
``e_<order>_<slug>`` / ``f_<order>_<slug>`` package scheme and the full
Epic/Feature → package path map are recorded in
``vibes/docs/specs/bounded-context-map.md`` (``## Redmine-numbered package path
map (#12622)``); the migration policy / facade idiom / residual policy are in
``vibes/docs/logics/source-layout-bounded-context-migration.md``
(``## #12622 Redmine-Numbered Layout Correction``). This supersedes the
#12590/#12594 ``features/governance_distribution/`` Epic-slug pilot, which is
abolished.

Modules relocated here are behavior-preserving moves; their former
``mozyo_bridge.application.<module>`` import paths remain available as
``sys.modules`` compatibility facades (the #12492/#12493/#12570 facade idiom).
The packaged / fixed surfaces ``scaffold/`` and ``docs_tools/`` (package-data and
explicit ``fc-*`` catalog entries) are held this round as residual and routed to
final integration #12630; this Epic round moves only the pure-Python governance
application-layer bodies.
"""
