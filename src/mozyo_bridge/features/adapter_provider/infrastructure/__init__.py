"""``adapter_provider`` infrastructure layer (Redmine Epic #12504).

The Redmine adapter implementation seam: the ticket provider
(``redmine_ticket_provider``), the journal note transport
(``redmine_note_transport``), the API context / env resolution
(``redmine_context``), and credential resolution (``redmine_credentials``). The
two former loose top-level modules (``mozyo_bridge.redmine_context`` /
``mozyo_bridge.redmine_credentials``) and the two former
``mozyo_bridge.infrastructure.<module>`` modules each keep their legacy import
path as a ``sys.modules`` facade (US #12595, source-layout expansion). No external
write / network behavior is added by the move; the seam stays read-only-by-design
per ``vibes/docs/logics/plugin-ready-adapter-boundary.md``.
"""
