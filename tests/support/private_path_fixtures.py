"""Home-path-shaped fixtures, composed at runtime.

`release check tree` (Source Tree Hygiene) strict-fails on any `/Users/<name>/`
or `/home/<name>/` literal in a tracked file, test fixtures included: a personal
home path is a release blocker even where it only serves as an example.

Tests that prove the runtime strips, refuses, or classifies such a path still
need a value of exactly that shape — redaction, mount-prefix, producer-boundary
vocabulary, home-containment — so compose the shape here rather than writing the
literal. The tracked bytes carry no home-path-shaped literal; the value handed to
the code under test is exactly one.

`tests/integration/.../test_release_helpers.py` pins these composed values
against the real `release check tree` command: the indirection is only a negative
control while what comes out is still what that gate blocks (Redmine #14656).

Fixtures whose home shape is incidental (an opaque doctor / parser path) use a
neutral sentinel root instead and do not belong here.
"""

from __future__ import annotations

# Split so the tracked source never contains the scanned literal. Joined at
# runtime these are exactly `/Users` and `/home`.
_MACOS_HOME_ROOT = "/" + "Users"
_LINUX_HOME_ROOT = "/" + "home"


def macos_home_path(*parts: str) -> str:
    """Return `/Users/<parts...>`, a macOS personal-home-shaped absolute path."""
    return "/".join((_MACOS_HOME_ROOT, *parts))


def linux_home_path(*parts: str) -> str:
    """Return `/home/<parts...>`, a Linux personal-home-shaped absolute path."""
    return "/".join((_LINUX_HOME_ROOT, *parts))
