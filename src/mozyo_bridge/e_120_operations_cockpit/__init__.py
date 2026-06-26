"""``e_120_operations_cockpit`` Epic package (Redmine Epic #12502, order 120).

Operations-cockpit bounded context migrated to the #12622 Redmine-numbered
Epic/Feature layout by US #12625 (parent US #12622, Feature #12533). Holds the
cockpit read model (``f_110_cockpit_read_model``), served Web UI
(``f_120_cockpit_web_ui``), cockpit actions / preflight
(``f_130_cockpit_actions_preflight``), presentation grouping / layout
(``f_140_presentation_grouping_layout``), and attention / freshness projection
(``f_150_attention_freshness_projection``). Legacy ``mozyo_bridge.<layer>.<module>``
import paths stay available as ``sys.modules`` facades. Epic/Feature -> package
mapping authority: ``vibes/docs/specs/bounded-context-map.md``
``## Redmine-numbered package path map (#12622)``.
"""
