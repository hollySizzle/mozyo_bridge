"""``f_160_provider_registry`` feature package (Redmine Feature #12529 ``160_ProviderRegistry``).

Provider registry for the adapter / provider context (Epic #12504): the registry domain
(``domain/provider_registry``, forbidden-authority guardrails) and the provider runtime
wiring (``application/provider_runtime``). #12529 is classified as ``adapter_provider``,
not ``quality_architecture`` (#12623 j#66018 coordinator finding). Legacy
``mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry`` / ``mozyo_bridge.application.provider_runtime``
import paths preserved as ``sys.modules`` facades (US #12627).
"""
