"""Machine-readable capability contract for ``project-gateway handoff`` (#15420).

A remote Unit Board action is delivered by running the TARGET environment's own
``project-gateway handoff``. The client deliberately reads none of that
environment's configuration (privacy boundary, #15138), so it cannot know
whether the remote CLI already resolves the receiver from the scope's
``provider_binding`` (an omitted ``--to``) or still requires the historical
explicit ``--to``. Guessing either way fails one cohort: pinning ``--to codex``
permanently refuses claude-bound scopes (#15414's gate), and omitting ``--to``
against an old CLI dies in argparse — an outcome the client can only classify
as uncertain, never as the version skew it actually is.

So the capability is ADVERTISED, the same way the launcher attestation schema
contract does it (Redmine #13847, :mod:`herdr_launcher_capability`): the
``project-gateway handoff`` parser emits a whitespace-free token line in its
``--help`` epilog (``RawDescriptionHelpFormatter`` keeps it verbatim), and the
client probes ``project-gateway handoff --help`` read-only before delivering.
A missing / malformed advertisement is *unprovable*, never salvaged into a
capability — the client then refuses typed and sends nothing, instead of
silently falling back to a receiver pin.

Pure: no I/O, no argparse dependency beyond the string it renders.
"""

from __future__ import annotations

import re

#: The verbatim epilog line prefix. Whitespace-free so one line survives every
#: help reflow / pager / SSH capture path byte-identically.
GATEWAY_HANDOFF_CAPABILITY_PREFIX = "mozyo_gateway_handoff_capability="

#: The receiver is resolved from the target scope's ``provider_binding`` when
#: ``--to`` is omitted; an explicit ``--to`` is verified against that binding.
CAPABILITY_BINDING_RECEIVER_V1 = "binding_receiver_v1"

#: A capability token names one contract: lowercase ASCII word characters only.
#: Anything else (embedded whitespace, an empty token, uppercase noise) is not a
#: token this module ever renders, so it is not one it recognises either.
_TOKEN_SHAPE = re.compile(r"^[a-z0-9_]+$")


def build_gateway_handoff_capability_epilog() -> str:
    """The verbatim epilog block advertising this CLI's handoff capabilities."""
    return f"{GATEWAY_HANDOFF_CAPABILITY_PREFIX}{CAPABILITY_BINDING_RECEIVER_V1}"


def gateway_handoff_capabilities(help_output: object) -> frozenset[str]:
    """Every well-formed capability token advertised in one ``--help`` capture.

    A line counts only when, stripped, it is exactly ``<prefix><token>`` with a
    canonical token shape. Malformed lines are ignored rather than repaired: an
    advertisement the renderer could not have produced proves nothing.
    """
    if not isinstance(help_output, str):
        return frozenset()
    found: set[str] = set()
    for raw_line in help_output.splitlines():
        line = raw_line.strip()
        if not line.startswith(GATEWAY_HANDOFF_CAPABILITY_PREFIX):
            continue
        token = line[len(GATEWAY_HANDOFF_CAPABILITY_PREFIX):]
        if _TOKEN_SHAPE.match(token):
            found.add(token)
    return frozenset(found)


def supports_binding_receiver(help_output: object) -> bool:
    """Whether the probed CLI resolves the receiver from ``provider_binding``."""
    return CAPABILITY_BINDING_RECEIVER_V1 in gateway_handoff_capabilities(help_output)


__all__ = (
    "CAPABILITY_BINDING_RECEIVER_V1",
    "GATEWAY_HANDOFF_CAPABILITY_PREFIX",
    "build_gateway_handoff_capability_epilog",
    "gateway_handoff_capabilities",
    "supports_binding_receiver",
)
