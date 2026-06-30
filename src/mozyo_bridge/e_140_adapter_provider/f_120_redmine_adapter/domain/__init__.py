"""Domain layer for the Redmine adapter feature (Feature #12525 ``120_RedmineAdapter``).

Pure, network-free Redmine-Version policy: open-leaf enumeration over a flat
issues snapshot (:mod:`redmine_version_enumeration`) and the fail-closed
rename / close / lock / delete preflight (:mod:`redmine_version_operation`).
Both declare read/write ports so a live HTTP adapter can drop in behind the same
seam without changing the policy. Nothing here performs I/O or mutates Redmine.
"""
