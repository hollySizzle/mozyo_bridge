"""``f_140_presentation_provider`` feature package (Redmine Feature #12527 ``140_PresentationProvider``).

Presentation provider seam for the adapter / provider context (Epic #12504): the
presentation provider protocol (``domain/presentation_adapter``) and the provider runtime
selection / wiring (``application/presentation_runtime``). The runtime wires
operations-cockpit-owned attention presentation providers without owning them. Legacy
``mozyo_bridge.domain.presentation_adapter`` / ``mozyo_bridge.application.presentation_runtime``
import paths preserved as ``sys.modules`` facades (US #12627).
"""
