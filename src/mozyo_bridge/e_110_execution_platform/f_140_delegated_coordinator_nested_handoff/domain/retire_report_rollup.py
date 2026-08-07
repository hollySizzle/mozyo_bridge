"""Pure report folds for the workspace supervisor's automatic-retire leg (#15066)."""

from __future__ import annotations


def ran(workspaces) -> bool:
    return any(bool(getattr(ws, "retire_ran", False)) for ws in workspaces)


def candidates(workspaces) -> int:
    return sum(len(getattr(ws, "retire_attempts", ()) or ()) for ws in workspaces)


def mutations(workspaces) -> int:
    return sum(int(getattr(ws, "retire_mutations", 0) or 0) for ws in workspaces)


def uncertain(workspaces) -> int:
    return sum(
        1
        for ws in workspaces
        if str(getattr(ws, "retire_disposition", "") or "")
        == "retire_leg_error"
        or any(bool(a.get("uncertain")) for a in (getattr(ws, "retire_attempts", ()) or ()))
    )


def payload(workspaces) -> dict[str, object]:
    return {
        "ran": ran(workspaces),
        "candidates": candidates(workspaces),
        "mutations": mutations(workspaces),
        "uncertain": uncertain(workspaces),
    }


def has_work(workspaces) -> bool:
    result = payload(workspaces)
    return bool(
        result["candidates"] or result["mutations"] or result["uncertain"]
    )


__all__ = ("ran", "candidates", "mutations", "uncertain", "payload", "has_work")
