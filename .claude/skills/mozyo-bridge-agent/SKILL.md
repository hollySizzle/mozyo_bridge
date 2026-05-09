---
name: mozyo-bridge-agent
description: Follow the mozyo_bridge project workflow for Asana-driven work, Notion rules, release checks, and safe tmux notification handling. Use when working in the mozyo_bridge repository, preparing PyPI/TestPyPI releases, updating agent rules, or coordinating Claude/Codex work through mozyo-bridge.
---

# mozyo-bridge-agent

This is the Claude Code project-skill adapter. The shared skill source lives at:

`skills/mozyo-bridge-agent/SKILL.md`

## Instructions

1. Confirm Claude Code was started from the target project root that contains this `.claude/skills/` directory.
2. Read `skills/mozyo-bridge-agent/SKILL.md` from the same project root.
3. If the shared skill source is missing, stop and tell the user to install or sync the project skill before continuing.
4. Follow the shared skill's core workflow.
5. Read only the referenced files needed for the current task.
6. Do not duplicate long-form rules into this adapter.

Claude-specific metadata or tool restrictions may be added here only when they are intentionally not shared with Codex.
