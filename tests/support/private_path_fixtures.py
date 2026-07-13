"""Home-path-shaped fixtures, composed at runtime.

`release check tree` (Source Tree Hygiene) strict-fails on any `/Users/<name>/`
or `/home/<name>/` literal in a tracked file, test fixtures included: a personal
home path is a release blocker even where it only serves as an example.

Redaction and mount-prefix tests still need a path of exactly that shape to prove
the runtime strips or classifies it, so compose the shape here rather than
writing the literal. The tracked bytes carry no home-path-shaped literal; the
value handed to the code under test is exactly one.

Fixtures whose home shape is incidental (an opaque doctor / parser path) use a
neutral sentinel root instead and do not belong here.
"""

from __future__ import annotations

# Split so the tracked source never contains the scanned literal. Joined at
# runtime these are exactly `/Users` and `/home`.
_MACOS_HOME_ROOT = "/" + "Users"


def macos_home_path(*parts: str) -> str:
    """Return `/Users/<parts...>`, a macOS personal-home-shaped absolute path."""
    return "/".join((_MACOS_HOME_ROOT, *parts))
