"""``application`` layer leaf of the ``f_160_release_version_governance`` Feature.

R1 layer-leaf shape: the DDD technical layer is kept as a path component under
the Feature package so same-named modules across layers do not collide and the
legacy ``mozyo_bridge.application.<module>`` import paths can be re-bound verbatim
via ``sys.modules`` facades. Houses the relocated governance release
application-layer modules (``release`` / ``cli_release``).
"""
