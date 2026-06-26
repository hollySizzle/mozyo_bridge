"""``f_120_redmine_adapter`` feature package (Redmine Feature #12525 ``120_RedmineAdapter``).

Redmine ticket adapter seam for the adapter / provider context (Epic #12504): API
context / env resolution (``infrastructure/redmine_context``), credential resolution
(``infrastructure/redmine_credentials``), note transport (``infrastructure/redmine_note_transport``),
and the ticket provider (``infrastructure/redmine_ticket_provider``). The seam stays
read-only-by-design; no external write / network behavior is added by the move. Legacy
``mozyo_bridge.infrastructure.<module>`` and top-level ``mozyo_bridge.redmine_context`` /
``mozyo_bridge.redmine_credentials`` import paths preserved as ``sys.modules`` facades
(US #12627).
"""
