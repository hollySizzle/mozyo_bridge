# Project Map Reference

## Repository

- Project: `mozyo-bridge`
- Import package: `mozyo_bridge`
- Package name: `mozyo-bridge`
- Repository: https://github.com/hollySizzle/mozyo_bridge
- Workspace: repository root
- Ticket system for `mozyo_bridge`: Redmine project `giken-3800-mozyo-bridge` (preset `redmine-governed`); the durable work record is the Redmine issue / journal.
- Asana project: configure per user or private workspace (used by adopting repos whose central preset is `asana`, not by `mozyo_bridge` itself).

## Important Paths

- `src/mozyo_bridge/`: package implementation
- `tests/`: unit tests
- `smoke/real_tmux_notify_smoke.py`: real tmux notification smoke test
- `.github/workflows/test.yml`: CI test workflow
- `.github/workflows/testpypi.yml`: TestPyPI publish workflow
- `.github/workflows/publish.yml`: production PyPI publish workflow
- `pyproject.toml`: package metadata
- `README.md`: user-facing usage and safety notes
- `.env.example`: local environment example with no secrets

## Documentation

- `vibes/docs/`: project documentation namespace, not a runtime namespace.
- `skills/mozyo-bridge-agent/`: shared skill source for Claude/Codex workflow guidance.
- `.claude/skills/mozyo-bridge-agent/`: Claude Code project-skill adapter.
