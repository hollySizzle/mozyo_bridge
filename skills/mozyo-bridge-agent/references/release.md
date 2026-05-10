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

1. Start from an Asana release task.
2. Run local unit tests and build checks.
3. Push to `main` and confirm GitHub Actions `Test` succeeds.
4. Use `Publish to TestPyPI` for TestPyPI.
5. Validate TestPyPI install with `pipx`.
6. Treat internal beta distribution as complete after TestPyPI install validation.
7. Decide production PyPI release separately and only when explicitly requested.

For TestPyPI validation, force the pip backend so TestPyPI is used for
`mozyo-bridge` and PyPI remains available for dependencies:

```bash
pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==X.Y.Z
```

Do not create a GitHub Release for internal beta distribution. The production
publish workflow runs on `release: published`, so a GitHub Release is a
production trigger.

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
