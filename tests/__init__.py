"""Test package root.

Bootstraps the repo-local ``src/`` directory onto ``sys.path`` so that
``import mozyo_bridge`` resolves to this checkout regardless of the order in
which ``unittest discover`` imports the test modules. The flat layout relied on
most test modules each inserting ``ROOT/"src"`` themselves; after the
type-first / bounded-context migration (Redmine #12490) the tests live in
``tests/<type>/<context>/`` packages, so the bootstrap is centralised here and
runs once when the ``tests`` package is first imported — before any submodule.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
