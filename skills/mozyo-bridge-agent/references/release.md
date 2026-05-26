# Release Reference

## Standard Verification

Use the smallest check set that matches the change.

```bash
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w /tmp/mozyo_bridge_dist
python -m mozyo_bridge --help
```

Use a Python environment matching the project's supported Python versions for local tests.

## tmux Delivery Changes

Run real smoke checks when changing tmux delivery, pane resolution, marker safety, or CLI notification contracts.

```bash
python smoke/real_tmux_notify_smoke.py
MOZYO_BRIDGE_COMMAND=mozyo-bridge-testpypi python smoke/real_tmux_notify_smoke.py
```

## Release Flow

1. Start from a release ticket in the active ticket system (Redmine issue for `mozyo_bridge`; Asana task for Asana-preset repos).
2. Run local unit tests and build checks.
3. Run release artifact guardrails.
4. Push to `main` and confirm GitHub Actions `Test` succeeds.
5. Use `Publish to TestPyPI` for TestPyPI.
6. Validate TestPyPI install with `pipx`.
7. Treat internal beta distribution as complete after TestPyPI install validation.
8. Decide production PyPI release separately and only when explicitly requested.

## Release Artifact Guardrails

Do not rely on `mozyo-bridge --version` alone. It reports the package version
from `pyproject.toml`, so GitHub `main`, TestPyPI, and PyPI can share the same
version string while shipping different command, preset, or skill content.

Before release, inspect all three surfaces:

- Source tree: search for credentials, tokens, `.env` / `.pypirc` content, and
  host-specific absolute paths such as `/Users/<name>`, `/home/<name>`, or
  `C:\Users\<name>`. Personal home paths are release blockers in public refs
  even when they are not secrets.
- Fresh scaffold output: with an isolated `--home` and isolated target, run
  `rules install`, scaffold `asana`, `redmine`, and `none`, then confirm
  generated `AGENTS.md`, `CLAUDE.md`, and `.mozyo-bridge/scaffold.json` contain
  `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md`
  and no resolved user-home path. `scaffold status` must report clean.
- Build artifacts: build both wheel and sdist, extract them, and scan the
  extracted files. Do not inspect the wheel only; sdist can include root docs.

Record any false positives and their rationale in the active release ticket (Redmine journal on the release issue, or an Asana comment on the release task, depending on the repo's central preset).

## Release Ref Consistency

- Keep version bumps as standalone commits.
- For a tagged release, install scripts and the skill tree must be fetched from
  the same tag as the package version under test. Set
  `MOZYO_BRIDGE_SKILL_REF=vX.Y.Z` for the fresh install smoke.
- Confirm the remote tag points to the intended release commit with
  `git ls-remote origin refs/tags/vX.Y.Z`.
- Do not mix a TestPyPI / PyPI package with install scripts from floating
  `main` when claiming release acceptance.

For TestPyPI validation, force the pip backend so TestPyPI is used for
`mozyo-bridge` and PyPI remains available for dependencies:

```bash
pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==X.Y.Z
```

Do not create a GitHub Release for internal beta distribution. The production
publish workflow runs on `release: published`, so a GitHub Release is a
production trigger.

## Distribution Gates

- Internal beta distribution uses TestPyPI, not production PyPI.
- Before calling an internal beta ready, install the package from TestPyPI with
  the same command given to beta testers.
- Do not substitute a local checkout, editable install, or local wheel for the
  beta tester path.
- Confirm both command entry points start: `mozyo-bridge --help` and
  `mozyo --help`.
- Confirm distributed scaffold/rule content that is material to the change is
  present inside the installed package.
- Confirm `rules install`, per-preset scaffold, `scaffold status`, and
  `doctor --target` work from the fresh TestPyPI / PyPI install path.
- Production PyPI distribution is separate from internal beta distribution and
  requires an explicit production release request or approval.

## Trusted Publishing

TestPyPI pending publisher:

- Project: `mozyo-bridge`
- Owner: `hollySizzle`
- Repository: `mozyo_bridge`
- Workflow: `testpypi.yml`
- Environment: `testpypi`

PyPI production publisher:

- Project: `mozyo-bridge`
- Owner: `hollySizzle`
- Repository: `mozyo_bridge`
- Workflow: `publish.yml`
- Environment: `pypi`
