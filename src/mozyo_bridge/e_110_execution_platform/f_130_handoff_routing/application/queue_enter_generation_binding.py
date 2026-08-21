"""Pure launch-generation binding predicates for the queue-enter rail (Redmine #15842).

Move-only extraction from ``handoff_herdr_queue_enter_rail`` (Redmine #15227 wrote the
bodies; nothing here is new behaviour). The rail sits exactly at its module-health
allowlist baseline, so #15842's deterministic submit proof had to land in a sibling
rather than as a self-approved baseline bump — the same posture ``injection_stage``
records for ``handoff.py`` ("a module sitting at its module-health baseline takes new
prose in a sibling instead of a self-approved baseline bump"). These predicates were
chosen because they are the rail's only fully pure, session-independent block: no
``HerdrQueueEnterSession`` state, no ops port, no transport in reach.

The rail re-imports all three under their previous private names, so every existing
call site and the ``#15227`` regression that imports them from the rail are unchanged.

What the three answer:

- :func:`canonical_private_generation_binding` — is this the exact action-time,
  terminal-bearing binding shape, with the ``process_generation`` encoding actually
  recomputed rather than trusted?
- :func:`same_terminal_generation` — do two such bindings name the same terminal
  generation, allowing the mutable ``row_revision`` to drift?
- :func:`public_generation_binding` — project the private join onto the
  redaction-safe ledger shape (terminal id and the generation encoding are private
  destructive-edge evidence and never enter public telemetry).
"""
from __future__ import annotations

#: The identity fields that must be byte-identical for two bindings to name the same
#: terminal generation. ``row_revision`` is deliberately absent: it is a mutation fence
#: that legitimately advances after body rendering, not a process-generation id.
STABLE_GENERATION_FIELDS = (
    "provider",
    "assigned_name",
    "locator",
    "terminal_id",
    "startup_action_id",
)

#: The redaction-safe subset published to telemetry / the delivery ledger.
PUBLIC_GENERATION_FIELDS = (
    "provider",
    "assigned_name",
    "locator",
    "row_revision",
    "attestation_observed_at",
    "startup_action_id",
)


def canonical_private_generation_binding(binding: object) -> bool:
    """Validate the terminal-bearing action-time shape without rendering it."""
    if not isinstance(binding, dict):
        return False
    required = {
        "provider", "assigned_name", "locator", "terminal_id", "row_revision",
        "process_generation", "attestation_observed_at", "startup_action_id",
    }
    if set(binding) != required:
        return False
    if any(
        type(binding[field]) is not str
        or not binding[field]
        or binding[field].strip() != binding[field]
        for field in required
    ):
        return False
    revision = binding["row_revision"]
    if any(char not in "0123456789" for char in revision):
        return False
    if len(revision) > 1 and revision.startswith("0"):
        return False
    name = binding["assigned_name"]
    terminal = binding["terminal_id"]
    locator = binding["locator"]
    expected = (
        f"{len(name)}:{name}:{len(terminal)}:{terminal}:"
        f"{len(locator)}:{locator}:r{revision}"
    )
    return binding["process_generation"] == expected


def same_terminal_generation(left: object, right: object) -> bool:
    """Compare stable v2 identity while allowing mutable terminal revision drift."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        canonical_private_generation_binding(left)
        and canonical_private_generation_binding(right)
        and all(left[field] == right[field] for field in STABLE_GENERATION_FIELDS)
    )


def public_generation_binding(binding: dict[str, str]) -> dict[str, str]:
    """Project the private terminal join onto the redaction-safe ledger shape."""
    return {field: binding[field] for field in PUBLIC_GENERATION_FIELDS}


__all__ = (
    "PUBLIC_GENERATION_FIELDS",
    "STABLE_GENERATION_FIELDS",
    "canonical_private_generation_binding",
    "public_generation_binding",
    "same_terminal_generation",
)
