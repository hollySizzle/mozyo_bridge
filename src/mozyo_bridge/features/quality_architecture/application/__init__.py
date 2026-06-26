"""``quality_architecture`` application layer (Redmine Epic #12505).

Application / CLI composition for the quality / architecture governance context:
the ``mozyo-bridge health`` argument parser (``cli_module_health``) and its
command handlers (``commands_module_health``). Relocated from the technical-layer
``mozyo_bridge.application`` package by US #12596; the legacy
``mozyo_bridge.application.<module>`` import paths stay live as ``sys.modules``
facades.
"""
