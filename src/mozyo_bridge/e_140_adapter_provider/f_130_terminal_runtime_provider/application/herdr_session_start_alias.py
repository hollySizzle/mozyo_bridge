"""Launch-time nested-workspace alias admission for session-start (#15190).

Kept in its own module rather than inside :mod:`.herdr_session_start`, which
sits exactly at the 1000-line module-health ceiling: the #13882 split already
established that this component grows by adding siblings, not by thickening the
use case. It is also the honest boundary — resolving *which workspace a launch
belongs to* is a separate decision from admitting and actuating that launch.

The rail this serves: ``--repo`` / ``MOZYO_REPO`` short-circuit in
:func:`shared.paths.resolve_repo_root` before any canonicalization, so an
explicit *nested* workspace root (a Rails application root inside a repo whose
canonical root already owns the default coordinator pair) reaches session-start
as an independent workspace and can plan a second default Codex/Claude pair for
one repository. Ordinary cwd resolution never had that hole — it is
Git-root-first (#13641).
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)


def apply_workspace_alias(repo_root: Path) -> Path:
    """Fold an explicitly-supplied nested root into its canonical root.

    Called from **both** launch entries, which is deliberate (review j#102107
    Finding 4):

    - :func:`...herdr_session_start.prepare_session` — the public entry. Calling
      it there, ahead of request validation, the home lock, binary resolution and
      the capability probe, is what makes a declined workspace zero-launch *and*
      zero-side-effect.
    - :func:`...herdr_session_start._prepare_session_locked` — the entry every
      launch actually reaches. The v1 replacement driver
      (``sublane_actuator_herdr_ops`` via
      ``prepare_actuator_lane_session(admission_lock_held=True)``) calls it
      directly, skipping the public lock wrapper it already holds the lock for,
      so a rail placed only on the public entry is bypassed by exactly the live
      replacement path.

    Re-applying is idempotent — a canonical root declares nothing, so an
    already-folded root resolves to itself — and the duplication is load-bearing:
    removing either call re-opens a bypass. So the rail holds identically at plan
    time (``--dry-run``) and at live action time.

    Returns the root a launch should use:

    - no declaration → the requested root, unchanged (the common path);
    - verified alias → the canonical root;

    and raises :class:`HerdrSessionStartError` otherwise. Every refusal carries a
    fixed typed reason token, and none of them falls back to the nested root:
    that fallback is exactly the duplicate-pair defect this rail removes.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.workspace_alias import (  # noqa: E501
        resolve_launch_root,
    )
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
        ALIAS_RELATIVE,
        STATE_LAUNCH_DISABLED,
        STATE_NO_DECLARATION,
    )

    resolution = resolve_launch_root(repo_root)
    if resolution.state == STATE_NO_DECLARATION:
        # Return the caller's OWN path object, not the resolver's normalized one.
        # `resolve_launch_root` resolves symlinks and `..` to compare roots, but an
        # undeclared workspace must come back byte-identical: the launch cwd is
        # spelled into the `herdr pane split --cwd` argv, and silently
        # canonicalizing it would change that argv for every workspace in the
        # world that declares nothing — the exact opposite of this rail's promise
        # to leave the common path untouched.
        return repo_root
    if resolution.ok:
        return Path(resolution.launch_root) if resolution.launch_root else repo_root
    if resolution.state == STATE_LAUNCH_DISABLED:
        raise HerdrSessionStartError(
            f"workspace {repo_root} declares launch-disabled "
            f"(reason: {resolution.reason}; {resolution.detail}). No agent was "
            f"launched. The declaration is {ALIAS_RELATIVE} in that workspace; "
            f"inspect it with `mozyo-bridge workspace alias show --repo "
            f"{repo_root}` or remove it with `mozyo-bridge workspace alias clear "
            f"--repo {repo_root}`."
        )
    raise HerdrSessionStartError(
        f"workspace {repo_root} carries an alias declaration that could not be "
        f"verified (reason: {resolution.reason}; {resolution.detail}). Failing "
        f"closed: no agent was launched, and the nested root was NOT used as a "
        f"fallback because that is the duplicate-pair defect this rail removes. "
        f"Re-declare it with `mozyo-bridge workspace alias set --repo {repo_root} "
        f"--to <canonical-root>`, or remove {ALIAS_RELATIVE} to restore "
        f"independent-workspace behavior."
    )


__all__ = ("apply_workspace_alias",)
