"""unit — operations_cockpit residual tests (Redmine #12488 context map).

US #12625 moved the operations_cockpit unit/integration tests into the numbered
``tests/<type>/e_120_operations_cockpit/f_<order>_<feature>/`` layout. The single
file kept here is residual: ``test_presentation_state`` exercises
``mozyo_bridge.presentation_state`` (the ``core/state`` desired-presentation store
held as a fixed surface this round, not one of the five operations_cockpit
Features), so its move is deferred to #12630 integration alongside the
``fc-presentation-state-db-source`` catalog re-point.
"""
