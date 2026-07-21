"""Hermetic agent-provider executables for launch tests (Redmine #13441).

Since #13441 the launch chokepoints render ``argv[0]`` as the provider's **verified
absolute executable**, resolved from the trusted environment (Design Answer j#76725
Q1). A launch test therefore can no longer assert a bare ``"claude"`` — but it must
also never assert the *host's* real path, which would make the suite depend on the
developer's machine (and on whether ``codex`` is installed at all).

This helper gives tests the third option the Design Answer asks for: an **injected
resolver environment**. It materializes real executable files in a temp directory and
returns a trusted ``PATH`` env pointing at them, so tests exercise the *real* resolver
end-to-end (override precedence, PATH safety, executable-bit + realpath verification,
ambiguity) while pinning a deterministic absolute ``argv[0]`` they own.

Typical use::

    binaries = FakeAgentBinaries(self.tmp_path)
    env = binaries.env()                       # {"PATH": "<tmp>/bin"}
    argv = build_agent_start_argv(..., env=env)
    self.assertEqual(argv[argv.index("--") + 1], binaries.path("claude"))
    self.assertEqual(argv[argv.index("--") + 2 :], ["--permission-mode", "auto"])

i.e. pin the exact absolute argv[0] the injected resolver returns, and pin the
remaining argv tokens as the byte-invariant suffix.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Mapping


#: The built-in providers every launch test needs resolvable.
DEFAULT_PROVIDER_COMMANDS: tuple[str, ...] = ("claude", "codex")


class FakeAgentBinaries:
    """Real (empty, executable) provider binaries under a temp ``bin`` directory.

    Real files, not mocks: the resolver verifies ``os.path.isfile`` + ``os.access(...,
    X_OK)`` against the symlink-resolved realpath, so a stub that only patched a
    function would skip exactly the verification this feature exists to perform.
    """

    def __init__(
        self,
        root: Path,
        commands: Iterable[str] = DEFAULT_PROVIDER_COMMANDS,
        *,
        bin_dir: str = "bin",
    ) -> None:
        self.bin_dir = Path(root) / bin_dir
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self._paths: dict[str, str] = {}
        for command in commands:
            self._paths[command] = self._install(command)

    def _install(self, command: str) -> str:
        target = self.bin_dir / command
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        # The resolver returns the realpath, so a temp dir behind a symlink (macOS
        # `/var` -> `/private/var`, which is exactly where `mkdtemp` lands) must be
        # resolved here too, or the expected value would never match.
        return os.path.realpath(str(target))

    def path(self, command: str) -> str:
        """The absolute realpath the resolver will return as ``argv[0]``."""
        return self._paths[command]

    def env(self, **extra: str) -> dict[str, str]:
        """A trusted env whose ``PATH`` is exactly this fixture's ``bin`` directory.

        Deliberately NOT the ambient ``PATH``: a launch test must not resolve the
        developer's real ``claude``. ``extra`` adds further env entries (e.g. a
        provider's trusted-override variable).
        """
        return {"PATH": str(self.bin_dir), **neutralized_overrides(), **extra}


def neutralized_overrides() -> dict[str, str]:
    """Every provider's trusted-executable override var, blanked out.

    A test that patches ``os.environ`` with ``clear=False`` inherits the developer's
    real environment. If that environment happens to set a provider's trusted override
    (``MOZYO_AGENT_CLAUDE_BINARY`` …), the override BEATS the hermetic ``PATH`` this
    fixture installs and the test silently resolves someone else's binary — or fails on
    a machine that sets it. Blanking each override (empty == unset to the resolver) makes
    the fixture's ``PATH`` authoritative regardless of the ambient environment.

    The variable NAMES come from the profile registry, so this stays correct if a
    profile renames its override or a new provider is added.
    """
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
        AGENT_PROVIDER_PROFILES,
    )

    return {p.executable.env_override: "" for p in AGENT_PROVIDER_PROFILES}


def fake_binaries_env(root: Path, **extra: str) -> tuple["FakeAgentBinaries", dict[str, str]]:
    """Convenience: the fixture plus its trusted env, in one call."""
    binaries = FakeAgentBinaries(root)
    return binaries, binaries.env(**extra)


#: One process-wide hermetic provider bin directory, for the many launch tests that only
#: need *some* resolvable provider binary and do not care where it lives. Sharing one set
#: keeps the stub paths stable across a module's tests (so they can be asserted) without
#: each test rebuilding them.
SHARED_PROVIDER_BINS = FakeAgentBinaries(
    Path(tempfile.mkdtemp(prefix="mzb-shared-provider-bins-"))
)
atexit.register(shutil.rmtree, SHARED_PROVIDER_BINS.bin_dir.parent, True)


def provider_bin_path(provider: str) -> str:
    """The absolute argv[0] the shared fixture resolves for ``provider``."""
    return SHARED_PROVIDER_BINS.path(provider)


def with_provider_path(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` made hermetic for provider resolution: fixture ``PATH``, no overrides.

    The ``PATH`` and the blanked overrides are applied LAST on purpose: an env copied
    from the real ``os.environ`` would otherwise keep the host's ``PATH`` (resolving the
    developer's real ``claude`` / ``codex``) or a stray trusted override that beats
    ``PATH`` entirely. Nothing about provider resolution may depend on the host
    (Redmine #13441 review R1-F4).
    """
    return {
        **(dict(env) if env else {}),
        "PATH": str(SHARED_PROVIDER_BINS.bin_dir),
        **neutralized_overrides(),
    }


def assert_argv0_is(testcase, argv: list[str], expected: str) -> None:
    """Assert ``argv[0]`` is the injected absolute executable (never a bare name)."""
    testcase.assertEqual(argv[0], expected)
    testcase.assertTrue(os.path.isabs(argv[0]), f"argv[0] must be absolute: {argv[0]!r}")


def path_env(env: Mapping[str, str]) -> str:
    """The ``PATH`` of a trusted env mapping (readability helper for assertions)."""
    return env.get("PATH", "")


__all__ = (
    "DEFAULT_PROVIDER_COMMANDS",
    "SHARED_PROVIDER_BINS",
    "FakeAgentBinaries",
    "assert_argv0_is",
    "fake_binaries_env",
    "path_env",
    "provider_bin_path",
    "with_provider_path",
)
