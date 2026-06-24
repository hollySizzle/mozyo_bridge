"""Test package root (Redmine #12490 type-first / bounded-context layout).

Note on src import: ``python -m unittest discover -s tests -v`` (the CI command)
uses ``tests`` as ``top_level_dir`` and imports modules as
``unit.<context>.test_*`` / ``integration.<context>.test_*`` (and recurses into
the ``scenarios`` / ``regressions`` / ``support`` subpackages added in Redmine
#12491) — it does **not** import this ``tests`` package, so code here does not
run under that command. Each test / support module therefore inserts the
repo-local ``src/`` onto ``sys.path`` itself, which keeps full discovery,
subpackage-scoped discovery, and single-file discovery all self-sufficient
regardless of import order.

The bootstrap below only takes effect when ``tests`` is imported *as a package*
(e.g. ``pytest`` with the project ``pythonpath``), and is a harmless no-op
otherwise.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
