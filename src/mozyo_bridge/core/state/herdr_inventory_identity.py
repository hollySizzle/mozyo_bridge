"""Strict completeness predicate for projected Herdr inventory authority (#15227)."""


def terminal_inventory_complete(view) -> bool:
    """Require complete canonical rows and global name/locator/terminal uniqueness."""
    agents = tuple(getattr(view, "agents", ()))
    raw_row_count = getattr(view, "raw_row_count", None)
    invalid_row_count = getattr(view, "invalid_row_count", None)
    axes = ("name", "locator", "terminal_id")
    return bool(
        getattr(view, "ok", False)
        and type(invalid_row_count) is int
        and invalid_row_count == 0
        and type(raw_row_count) is int
        and raw_row_count == len(agents)
        and all(
            all(
                type(getattr(agent, axis, None)) is str
                and getattr(agent, axis)
                and getattr(agent, axis).strip() == getattr(agent, axis)
                for axis in axes
            )
            for agent in agents
        )
        and all(
            len({getattr(agent, axis) for agent in agents}) == len(agents)
            for axis in axes
        )
    )


__all__ = ("terminal_inventory_complete",)
