"""``adapter_provider`` domain layer (Redmine Epic #12504).

Provider-neutral domain contracts for the adapter / provider context: the ticket
adapter common contract (``ticket_adapter``), the presentation provider protocol
(``presentation_adapter``), the plugin manifest / marketplace records
(``plugin_manifest``), and the provider registry (``provider_registry``). Each
module keeps its legacy ``mozyo_bridge.domain.<module>`` import path as a
``sys.modules`` facade (US #12595, source-layout expansion).
"""
