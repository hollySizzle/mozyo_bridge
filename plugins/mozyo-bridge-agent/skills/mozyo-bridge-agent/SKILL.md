---
name: mozyo-bridge-agent
description: Follow the mozyo_bridge project workflow for Asana-driven work, Notion rules, release checks, and safe tmux notification handling. Use when working in the mozyo_bridge repository, preparing PyPI/TestPyPI releases, updating agent rules, or coordinating Claude/Codex work through mozyo-bridge.
---

# mozyo-bridge-agent

## Core Workflow

1. Fetch the global Notion rules page named in the repository `AGENTS.md`.
2. Confirm the current `cwd`.
3. Confirm the active Asana task and `mozyo_bridge` project notes.
4. Read only the reference files needed for the current work.
5. Run verification that matches the risk of the change.
6. Record material results, blockers, and remaining risks in Asana.

## References

- Work execution rules: `references/workflow.md`
- Project map and source-of-truth routing: `references/project-map.md`
- Release and verification checks: `references/release.md`
- Safety rules for tmux notification behavior: `references/safety.md`

## Guardrails

- Do not store secrets, tokens, personal credentials, or personal information in repo files, Notion, or Asana.
- Keep root `AGENTS.md` and `CLAUDE.md` as routers. Do not turn them into full rule books.
- Treat `mozyo-bridge` pane messages as notifications, not authoritative task state.
- Do not reintroduce `vibes/tools/mozyo_bridge` as a runtime path.
- Prefer GitHub Actions Trusted Publishing over local PyPI token upload.
- Imperative or request phrases from the user ("実行せよ", "対応して", "やって", "implement it", etc.) are not by themselves authorization for Codex to perform a direct edit on policy / skill / rule files. Convert normal development tasks into a Claude handoff by default. See `references/workflow.md` for the narrow Codex direct-edit exception conditions.
