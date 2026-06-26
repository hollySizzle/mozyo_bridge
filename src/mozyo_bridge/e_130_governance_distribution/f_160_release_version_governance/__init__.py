"""``f_160_release_version_governance`` Feature package (Redmine Feature #12523, order 160).

Release / version-governance Feature of the ``e_130_governance_distribution``
Epic. Houses the relocated release helper surface and its CLI parser
(``release`` / ``cli_release``). ``release`` is allowlisted in
``module_health.yaml`` (oversized) and carries an explicit ``fc-release-source``
catalog entry; the catalog re-point for the new path is routed to #12630.
"""
