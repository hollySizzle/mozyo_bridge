# Release Reference

## Standard Verification

Use the smallest check set that matches the change.

```bash
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w /tmp/mozyo_bridge_dist
python -m mozyo_bridge --help
```

On this machine, Homebrew `python3` may point to Python 3.14 without `PyYAML`. Use a venv with dependencies installed when needed.

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
6. Decide production PyPI release only after TestPyPI validation.

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
