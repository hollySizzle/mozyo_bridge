"""tmux pane user-option projection for derived attention (Redmine #11954).

Builds the ``tmux set-option`` command plan that caches a #11951
:class:`~mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention.AttentionRecord` onto a pane's user
options. The plan builder is **pure** (returns argv tuples, runs no tmux), so
the projection's command generation is testable without a tmux server; the CLI
command (``agents attention-project``) previews or executes the plan.

Boundary: these user options are a **projection cache**, not the source of
truth. They are re-derivable from durable state / the read model, may be deleted
freely, and are never consulted for routing / handoff preflight / target
resolution. This module imports only the pure attention read model — no tmux
client, no routing, no ``agent-ui.conf`` / color.
"""

from __future__ import annotations

from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import AttentionRecord

# tmux pane user-option names from `vibes/docs/logics/cockpit-attention-state.md`
# (`### tmux user option`). These are cache markers; the design doc pins that
# they are not used for handoff preflight / routing.
ATTENTION_STATE_OPTION = "@mozyo_attention_state"
ATTENTION_SEVERITY_OPTION = "@mozyo_attention_severity"
ATTENTION_REASON_OPTION = "@mozyo_attention_reason"
ATTENTION_UPDATED_AT_OPTION = "@mozyo_attention_updated_at"

ATTENTION_OPTION_NAMES = (
    ATTENTION_STATE_OPTION,
    ATTENTION_SEVERITY_OPTION,
    ATTENTION_REASON_OPTION,
    ATTENTION_UPDATED_AT_OPTION,
)


def build_attention_option_plan(
    pane_id: str | None, record: AttentionRecord
) -> list[tuple[str, ...]]:
    """Return the ``tmux set-option -p`` argv tuples caching ``record`` on a pane.

    Pure: one ``("set-option", "-p", "-t", pane_id, name, value)`` tuple per
    attention user option, in a stable order, and nothing is executed. Returns
    an empty plan when ``pane_id`` is falsy (a candidate without a pane id cannot
    carry a pane option). Values are passed as argv elements, so no shell
    escaping is needed at execution time.
    """
    if not pane_id:
        return []
    pairs = (
        (ATTENTION_STATE_OPTION, record.attention_state),
        (ATTENTION_SEVERITY_OPTION, record.severity),
        (ATTENTION_REASON_OPTION, record.reason_code),
        (ATTENTION_UPDATED_AT_OPTION, record.observed_at or ""),
    )
    return [
        ("set-option", "-p", "-t", pane_id, name, value) for name, value in pairs
    ]
