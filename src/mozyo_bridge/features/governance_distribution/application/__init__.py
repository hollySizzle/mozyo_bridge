"""``application`` layer leaf of the ``governance_distribution`` context.

R1 layer-leaf shape (Redmine #12591 j#65435): the DDD technical layer is kept as
a path component under the Epic slug so that same-named modules across
``domain`` / ``application`` do not collide and the legacy
``mozyo_bridge.application.<module>`` import paths can be re-bound verbatim via
``sys.modules`` facades. Houses the relocated governance application-layer CLI /
command modules (`cli_release`, `cli_docs_scaffold`, `commands_docs_scaffold`).
"""
