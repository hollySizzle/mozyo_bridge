"""``adapter_provider`` application layer (Redmine Epic #12504).

Application-level runtime wiring for the adapter / provider context: the
presentation provider runtime (``presentation_runtime``) and the provider registry
runtime (``provider_runtime``). Each module keeps its legacy
``mozyo_bridge.application.<module>`` import path as a ``sys.modules`` facade
(US #12595, source-layout expansion).
"""
