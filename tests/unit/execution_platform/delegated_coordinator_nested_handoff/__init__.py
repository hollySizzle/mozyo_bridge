"""unit — execution_platform / delegated_coordinator_nested_handoff feature tests.

Redmine Feature #12510 `140_DelegatedCoordinator・NestedHandoff` (US #12570 tests
follow source pilot). Tests here drive the live executor relocated to
``mozyo_bridge.features.execution_platform.delegated_coordinator_nested_handoff``;
they import via the stable public path, which the legacy-path facade keeps valid.
"""
