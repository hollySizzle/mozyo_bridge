"""``f_150_skill_plugin_distribution`` Feature package (Redmine Feature #12522, order 150).

Skill / plugin distribution Feature of the ``e_130_governance_distribution``
Epic. Holds the authority for the legacy project Claude skill *partial* mirror
at ``.claude/skills/mozyo-bridge-agent/references/`` (Redmine #14580): the
mirror contract (rules A-F), the check / sync application service, and the CLI
adapter that ``scripts/sync_legacy_project_skill.sh`` execs into.

The mirror belongs to this Feature, not to release governance;
``f_160_release_version_governance/release_drift.py`` stays a *caller* that
bundles the gate and must not reproduce any mirror rule
(design consultation answer: Redmine #14580 journal #90402).
"""
