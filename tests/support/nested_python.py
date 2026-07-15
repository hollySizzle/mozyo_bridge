"""Env builder for tests that spawn a *nested* Python / pip subprocess.

A test that installs the built wheel into a throwaway venv is only meaningful if
that install is hermetic from the runner's own import setup. An inherited
``PYTHONPATH`` that carries this checkout's ``src/`` (or any dir holding
mozyo-bridge metadata) makes pip resolve the same version as already importable and
skip the install entirely — it exits 0, but no console script is written, so the
test then asserts against a venv that was never populated. That is exactly how the
parallel runner's old ``PYTHONPATH`` injection turned a green serial test red
(Redmine #13735 j#78390 F1).

The runner no longer injects ``PYTHONPATH`` (the shard's runtime is pinned
in-process instead), but a nested install must not depend on the *caller's* env
being clean either. Use :func:`hermetic_python_env` for any subprocess whose whole
point is to exercise an installed artifact.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def hermetic_python_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child env with ``PYTHONPATH`` dropped, so imports come from the target only.

    Everything else is inherited, so the child keeps the caller's isolated
    ``HOME`` / ``TMPDIR`` / ``MOZYO_BRIDGE_HOME`` (and, under the parallel runner,
    its ``PYTHONUSERBASE`` and git identity).
    """
    env = dict(os.environ if base is None else base)
    env.pop("PYTHONPATH", None)
    return env
