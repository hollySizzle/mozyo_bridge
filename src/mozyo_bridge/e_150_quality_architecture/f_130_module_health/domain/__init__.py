"""``f_130_module_health`` domain layer (Redmine Feature #12532, Epic #12505).

Pure domain logic for module-level governance: the module-health metrics +
oversized-module gate (``module_health``) and the built-in CLI module registry /
composition classification (``module_registry``). Relocated from
``features/quality_architecture/domain`` by #12628 (Redmine-numbered layout
correction). The legacy top-level ``mozyo_bridge.domain.<module>`` ``sys.modules``
facades were retired by #12632 (top-level residual removal); importers use the
Redmine-numbered path directly.
"""
