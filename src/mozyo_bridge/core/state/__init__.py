"""Managed-state / runtime foundational package (core.state).

Home of the managed desired-state event log, workspace registry, cross-workspace
session inventory, OTel store, workspace defaults, and presentation-state
projection. Relocated here from the former top-level ``mozyo_bridge`` modules by
Unit A of the source-layout bounded-context migration (Redmine #12493).

The pre-move import paths (``mozyo_bridge.state_store`` etc.) stay valid as
compatibility facades; their retirement is tracked separately via the
fallback-retirement-ledger process, not in this move.
"""
