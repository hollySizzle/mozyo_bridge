"""integration — adapter_provider shared-boundary residual tests (Redmine #12488 context map).

US #12627 (Redmine-numbered layout correction) moved the clearly-owned adapter_provider
tests into ``tests/integration/e_140_adapter_provider/f_<order>_<feature>/``. The two
``test_handoff_delivery_*`` modules remain here as a residual holding: their acceptance
subjects are ``mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff`` (execution_platform, Epic #12501 / Feature
#12509 HandoffRouting, owned by the #12624 lane) and ``mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink``
(a shared delivery-record seam), neither of which is in the adapter_provider source scope
(``vibes/docs/specs/bounded-context-map.md`` `### adapter_provider`). Final placement of
these cross-cutting tests is deferred to #12630 integration in coordination with #12624
(``vibes/docs/logics/source-layout-bounded-context-migration.md`` ambiguous-module policy).
"""
