---
name: mozyo-bridge-agent
description: Follow the mozyo_bridge project workflow for ticket-driven work (Redmine or Asana, per the repo's scaffold preset), preset rule fetches, release checks, and safe tmux notification handling. Use when working in the mozyo_bridge repository, preparing PyPI/TestPyPI releases, updating agent rules, or coordinating Claude/Codex work through mozyo-bridge.
---

# mozyo-bridge-agent

## Core Workflow

1. Fetch the repository's central preset rules named in `AGENTS.md` (the `mozyo_bridge` repo itself uses the `redmine-governed` preset; other adopting projects may use `redmine`, `asana`, or `none`).
2. Confirm the current `cwd`.
3. Confirm the active ticket in the repo's ticket system (Redmine issue / journal for Redmine-preset repos including `mozyo_bridge`; Asana task / comment for Asana-preset repos) and the project notes.
4. Read only the reference files needed for the current work.
5. Run verification that matches the risk of the change.
6. Record material results, blockers, and remaining risks in the active ticket system (Redmine journal or Asana comment, whichever the repo uses).

## References

- Work execution rules: `references/workflow.md`
- Redmine issue authoring granularity and Version operation: `references/redmine-issue-authoring.md`
- Project map and source-of-truth routing: `references/project-map.md`
- Release and verification checks: `references/release.md`
- Safety rules for tmux notification behavior: `references/safety.md`

## Guardrails

- Do not store secrets, tokens, personal credentials, or personal information in repo files, preset rule docs, or ticket-system entries (Asana tasks / comments, Redmine issues / journals).
- Keep root `AGENTS.md` and `CLAUDE.md` as routers. Do not turn them into full rule books.
- Treat `mozyo-bridge` pane messages as notifications, not authoritative task state.
- Prefer GitHub Actions Trusted Publishing over local PyPI token upload.
- Imperative or request phrases from the user ("実行せよ", "対応して", "やって", "implement it", etc.) are not by themselves authorization for Codex to perform a direct edit on policy / skill / rule files. Convert normal development tasks into a Claude handoff by default. See `references/workflow.md` for the narrow Codex direct-edit exception conditions.
