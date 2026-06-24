"""Test package root.

Enables ``unittest discover -s tests`` to recurse into the ``scenarios`` /
``regressions`` / ``support`` subpackages (Redmine #12491). ``discover`` uses
``tests`` as ``top_level_dir`` and does not import this module, so it carries no
bootstrap; each test / support module self-inserts the repo-local ``src``.
"""
