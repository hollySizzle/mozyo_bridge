"""``e_140_adapter_provider`` epic package (Redmine Epic #12504 ``140_Adapter・Provider基盤``, order 140).

Adapter / provider bounded context, re-homed for the US #12622 Redmine-numbered
layout correction (child US #12627). Splits into Feature-level numbered packages
``f_<order>_<feature_slug>/`` per ``vibes/docs/specs/bounded-context-map.md``
`## Redmine-numbered package path map (#12622)`: ticket adapter common (#12524 →
``f_110_ticket_adapter_common``), Redmine adapter (#12525 → ``f_120_redmine_adapter``),
Asana adapter seam (#12526 → ``f_130_asana_adapter``, currently no dedicated runtime
body), presentation provider (#12527 → ``f_140_presentation_provider``), plugin
manifest / marketplace (#12528 → ``f_150_plugin_manifest_marketplace``), and provider
registry (#12529 → ``f_160_provider_registry``).

Relocated modules are behavior-preserving moves; the legacy
``mozyo_bridge.<layer>.<module>`` (and top-level ``mozyo_bridge.redmine_context`` /
``redmine_credentials``) import paths stay as ``sys.modules`` facades. This supersedes
the #12590/#12595 ``features/adapter_provider/`` epic-slug pilot (``features/`` root
abolished); facade retirement remains a separately-gated later stage
(``fallback-retirement-ledger``).
"""
