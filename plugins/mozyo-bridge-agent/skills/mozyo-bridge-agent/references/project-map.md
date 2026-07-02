# Project Map Reference

How an agent resolves the project map — important paths, documentation namespaces, and the ticket-system binding — for the repository it is working in. The map itself is repo-specific data and lives in the adopting repo, not in this distributed body.

## Resolving The Adopting Repo's Map

- Read the adopting repo's own project map from its local docs namespace; for a governed-scaffold repo, the docs catalog is the entry point (`mozyo-bridge docs resolve <path>` surfaces the registered map / spec docs for the paths you are touching).
- A useful project map covers: the package / import names, the repository and workspace root, the ticket system and project id selected by the repo's scaffold preset, the implementation / test / docs paths, CI workflows, and packaging metadata.
- Root `AGENTS.md` / `CLAUDE.md` stay thin routers; the map body belongs in the repo's local docs, and this reference does not duplicate it.

## mozyo_bridge (this repository) — worked example

The concrete map below is for the `mozyo_bridge` repository itself — the repo that develops mozyo-bridge — kept here as a worked example and for dogfooding sessions. An adopting project maintains its own equivalent in its local docs; do not copy these values.

### Repository

- Project: `mozyo-bridge`
- Import package: `mozyo_bridge`
- Package name: `mozyo-bridge`
- Repository: https://github.com/hollySizzle/mozyo_bridge
- Workspace: repository root
- Ticket system for `mozyo_bridge`: Redmine project `giken-3800-mozyo-bridge` (preset `redmine-governed`); the durable work record is the Redmine issue / journal.
- Asana project: configure per user or private workspace (used by adopting repos whose central preset is `asana`, not by `mozyo_bridge` itself).

### Important Paths

- `src/mozyo_bridge/`: package implementation
- `tests/`: unit tests
- `smoke/real_tmux_notify_smoke.py`: real tmux notification smoke test
- `.github/workflows/test.yml`: CI test workflow
- `.github/workflows/testpypi.yml`: TestPyPI publish workflow
- `.github/workflows/publish.yml`: production PyPI publish workflow
- `pyproject.toml`: package metadata
- `README.md`: user-facing usage and safety notes
- `.env.example`: local environment example with no secrets

### Documentation

- `vibes/docs/`: project documentation namespace, not a runtime namespace.
- `skills/mozyo-bridge-agent/`: shared skill source for Claude/Codex workflow guidance.
- `.claude/skills/mozyo-bridge-agent/`: Claude Code project-skill adapter.
