"""unit — ``mozyo_bridge.core`` (shared-kernel) tests.

This package marker was missing, so ``python -m unittest discover -s tests`` (the CI command)
silently skipped this entire ``tests/unit/core/`` subtree: discovery cannot recurse a non-package
directory, and unittest drops it without even an error placeholder. Omitting a subdirectory's
``__init__.py`` and leaving its tests false-green is a named anti-pattern in
``vibes/docs/logics/tests-placement-discovery-policy.md`` (`## Anti-patterns`), whose
`## #12490 migration contract` requires the marker in every subdirectory and lists its documented
exceptions as 4 exact flat paths — this subtree is not among them.

Measured when restored (Redmine #14701, review j#94494 F1 relocation): the 7 files under
``core/state/`` held **113 tests that had never run** under the discovery command since
``c25b1352`` created the directory — counted on the R1 head ``4526435b`` with unittest's own
loader: fence 26, forward_outbox 18, hibernate_redrive 17, lane_kind 4,
startup_execution_events 31, supervisor_lease 10, supervisor_wake 7. The **current** total is 116,
which is those 113 plus the 3 contract tests the F1 relocation added to the fence file; the two
numbers answer different questions and an earlier version of this docstring reported 116 as the
historical figure (review j#95727 F2). All pass.

The context axis in the placement policy names the six ``#12488`` bounded contexts and does not
name the shared kernel; ``core`` as a tests context is the pre-existing convention this directory
already followed and is not decided here.
"""
