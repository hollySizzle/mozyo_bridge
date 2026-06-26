"""``quality_architecture`` domain layer (Redmine Epic #12505).

Pure domain logic for the quality / architecture governance context: the
module-health metrics + oversized-module gate (``module_health``) and the
built-in CLI module registry / composition classification (``module_registry``).
Relocated from the technical-layer ``mozyo_bridge.domain`` package by US #12596;
the legacy ``mozyo_bridge.domain.<module>`` import paths stay live as
``sys.modules`` facades.
"""
