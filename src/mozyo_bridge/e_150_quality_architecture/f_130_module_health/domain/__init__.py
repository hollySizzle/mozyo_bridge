"""``f_130_module_health`` domain layer (Redmine Feature #12532, Epic #12505).

Pure domain logic for module-level governance: the module-health metrics +
oversized-module gate (``module_health``) and the built-in CLI module registry /
composition classification (``module_registry``). Relocated from
``features/quality_architecture/domain`` by #12628 (Redmine-numbered layout
correction); the legacy ``mozyo_bridge.domain.<module>`` import paths stay live as
``sys.modules`` facades.
"""
