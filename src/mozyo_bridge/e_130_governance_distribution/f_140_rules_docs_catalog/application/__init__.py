"""``application`` layer leaf of the ``f_140_rules_docs_catalog`` Feature.

R1 layer-leaf shape: the DDD technical layer is kept as a path component under
the Feature package so same-named modules across layers do not collide and the
legacy ``mozyo_bridge.application.<module>`` import paths can be re-bound verbatim
via ``sys.modules`` facades. Houses the relocated governance docs/scaffold
application-layer modules (``cli_docs_scaffold`` / ``commands_docs_scaffold``).
"""
