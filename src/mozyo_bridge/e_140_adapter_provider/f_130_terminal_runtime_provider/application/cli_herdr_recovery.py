"""One registration point for every herdr session RECOVERY surface (Redmine #13948).

``cli_core`` sits exactly at the 1000-line module-health ceiling, so a new public command
cannot simply add its own import + call there. #13892 already met this and answered it with
:func:`register_herdr_retirement_surfaces` — one entry point per operator story. This is
the same answer widened by one story: the composition root keeps its single call, and the
family it composes grows here instead.

The family is "what an operator does when a herdr session did not end up how it should":

- ``session-retire`` + ``retirement-store status`` (#13892) — retire a record-less scratch
  pair that is already there, and explain that rail's refusals;
- ``session-rollback`` (#13948) — converge the panes ONE session-start action started when
  that action did not report every requested role healthy;
- ``startup-status`` (#14231) — read where ONE action's launch actually stopped, including
  a generation that has already vanished from the live inventory. Read-only and
  diagnostic: it is the surface an operator consults BEFORE deciding whether a rollback is
  even the right rail.

They are siblings, not synonyms, and their authorities stay separate on purpose: a
retirement acts on a pair by identity and asks an owner about a pending composer; a
rollback acts only on its own action's participants and never extends that composer
authority (Answer j#80991); ``startup-status`` acts on nothing at all and grants no
authority to what it reports (Answer j#84724). Registering them together is a CLI-shape
decision only.
"""

from __future__ import annotations


def register_herdr_recovery_surfaces(herdr_sub, *, add_repo_option=None) -> None:
    """Register retirement (#13892), the rollback rail (#13948), and startup-status (#14231)."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_retirement_store import (  # noqa: E501
        register_herdr_retirement_surfaces,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_cli import (  # noqa: E501
        register_herdr_session_rollback_parser,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_status import (  # noqa: E501
        register_herdr_startup_status_parser,
    )

    register_herdr_retirement_surfaces(herdr_sub, add_repo_option=add_repo_option)
    register_herdr_session_rollback_parser(herdr_sub)
    register_herdr_startup_status_parser(herdr_sub)


__all__ = ("register_herdr_recovery_surfaces",)
