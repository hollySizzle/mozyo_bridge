"""``delegated_coordinator_nested_handoff`` feature-slug package.

Redmine Feature #12510 `140_DelegatedCoordinator・NestedHandoff` (order 140) under
Epic #12501 `execution_platform`. US #12570 source-layout pilot: the delegated
coordinator / nested-handoff slice migrates here from the technical-layer
``domain/`` / ``application/`` layout, one behavior-preserving module at a time,
each keeping its legacy ``mozyo_bridge.<layer>.<module>`` import path as a
``sys.modules`` facade.

Pilot scope (#12570): ``delegation_route_executor`` (the live executor on the
#12556/#12546 path). Expansion to the rest of the feature cluster
(``delegation_route_planner`` / ``delegation_route_records`` / ``route_identity_ledger``
/ ``delegation_*`` / ``grandchild_*``) is a deliberately-deferred follow-up
(Redmine #12570 j#65077 decision #6).
"""
