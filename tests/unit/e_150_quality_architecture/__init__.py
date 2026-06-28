"""unit — e_150_quality_architecture bounded-context tests.

Redmine Epic #12505 ``150_品質・アーキテクチャ統治``. Feature-owned tests nest
under ``f_<order>_<feature>/`` subpackages.

This package marker was missing, so ``python -m unittest discover -s tests`` (the
CI command) silently skipped this entire ``tests/unit/e_150_quality_architecture/``
subtree — discovery cannot recurse a non-package directory, and unittest drops it
without an error placeholder. That hid the ``f_150_ci_verification`` unit tests
(Redmine #12752 ``test_impact`` and #12754 ``test_runtime``) from CI. Restored in
Redmine #12754 so the test runtime summary measures the full suite. See
``vibes/docs/logics/tests-placement-discovery-policy.md`` on the per-dir
``__init__.py`` package-ization requirement.
"""
