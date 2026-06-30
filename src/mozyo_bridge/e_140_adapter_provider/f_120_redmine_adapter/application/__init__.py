"""Application layer for the Redmine adapter feature (Feature #12525).

Thin CLI wiring + handlers for the ``redmine-version`` family (Redmine #12651):
open-leaf enumeration and the fail-closed rename/close/lock/delete preflight over
operator-exported snapshots. Advisory only — it reads JSON snapshots and renders
a decision; it performs no Redmine write and touches no network.
"""
