#!/usr/bin/env python3
"""Shell-heavy driver for the installed fault smoke (Redmine #14097).

Split from ``installed_fault_smoke.py`` so that file keeps a small PURE decision surface the
hermetic unittest exercises without a real subprocess. This module OWNS the real
installed-``mozyo-bridge`` subprocess drives: each fault shape's public entrypoint (proving the
built artifact dispatches it) and a representative success path per driveable shape, every one
under an isolated ``MOZYO_BRIDGE_HOME`` + a secret-free temp fake-herdr state served by the
canonical fake through ``smoke/support/fake_herdr_cli.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support.herdr_fake import (  # noqa: E402
    STATUS_WORKING,
    FakeHerdr,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_FAKE_HERDR_CLI = _REPO_ROOT / "smoke" / "support" / "fake_herdr_cli.py"


def _base_env(home: Path, *, herdr_state: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "MOZYO_REPO")}
    env["MOZYO_BRIDGE_HOME"] = str(home)
    if herdr_state is not None:
        # The installed CLI shells out to this exact executable as its herdr binary (a directly
        # executable script via its shebang; the resolver verifies isfile + X_OK).
        env["MOZYO_HERDR_BINARY"] = str(_FAKE_HERDR_CLI)
        env["MOZYO_FAKE_HERDR_STATE"] = str(herdr_state)
    return env


def _run(cli: Path, argv: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([str(cli), *argv], capture_output=True, text=True, env=env)


def drive_entrypoints(cli: Path, tmp: Path) -> dict[str, int]:
    """Run each fault shape's installed public entrypoint (``--help``); returns {shape: rc}."""
    from installed_fault_smoke import SHAPE_ENTRYPOINTS

    home = tmp / "entry_home"
    home.mkdir(parents=True, exist_ok=True)
    env = _base_env(home)
    return {shape: _run(cli, list(argv), env).returncode for shape, argv in SHAPE_ENTRYPOINTS}


def _herdr_repo(tmp: Path, ws_id: str) -> Path:
    repo = tmp / "herdr_repo"
    (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text(
        "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    (repo / ".mozyo-bridge" / "workspace-anchor.json").write_text(
        json.dumps({
            "schema_version": 1, "workspace_id": ws_id, "canonical_session": "fixture_14097_smoke",
            "project_name": "mozyo-bridge", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    return repo


def drive_representative(cli: Path, tmp: Path) -> dict[str, bool]:
    """Drive a representative SUCCESS path per driveable shape through the installed artifact."""
    results: dict[str, bool] = {}

    # callback-lease: bootstrap the store, then a healthy status read (no herdr backend needed).
    cl_home = tmp / "cl_home"
    cl_home.mkdir(parents=True, exist_ok=True)
    env = _base_env(cl_home)
    boot = _run(cli, ["workflow", "callback-lease", "--bootstrap"], env)
    status = _run(cli, ["workflow", "callback-lease"], env)
    results["callback_lease"] = boot.returncode == 0 and status.returncode == 0

    # sublane list --json: the installed CLI reads the fake herdr inventory and must NOT leak a
    # locator-present shell-residue worker into a live pane (the #14063 projection).
    ws_id = "fixture-14097-smoke-workspace"
    repo = _herdr_repo(tmp, ws_id)
    home = tmp / "list_home"
    home.mkdir(parents=True, exist_ok=True)
    from mozyo_bridge.core.state.lane_metadata import record_lane_created

    record_lane_created(
        lane_workspace_token="issue_14097_smoke", repo_workspace_id=ws_id, issue_id="14097",
        lane_label="issue_14097_smoke", branch="issue_14097_smoke", lane_id="issue_14097_smoke",
        source_backend="herdr", home=home,
    )
    fake = FakeHerdr(read_text="idle\n> ")
    fws = fake.seed_workspace(cwd=str(repo))
    fake.seed_agent(encode_assigned_name(ws_id, "codex", "issue_14097_smoke"),
                    workspace_id=fws, provider="codex", status=STATUS_WORKING)
    fake.seed_agent(encode_assigned_name(ws_id, "claude", "issue_14097_smoke"),
                    workspace_id=fws, provider="", status="unknown", detected_agent="")
    state = tmp / "herdr_state.json"
    state.write_text(json.dumps(fake.to_state()), encoding="utf-8")
    out = _run(cli, ["sublane", "list", "--json", "--repo", str(repo)],
               _base_env(home, herdr_state=state))
    try:
        payload = json.loads(out.stdout)
        lane = next(la for la in payload["sublanes"] if la["lane_id"] == "issue_14097_smoke")
        # live gateway + stale worker => gateway_only, the stale worker never a live pane.
        results["sublane_list"] = (
            out.returncode == 0 and lane["state"] == "gateway_only"
            and lane["worker_pane"] is None and "worker_slot_stale" in lane["stale_hints"]
        )
    except (ValueError, StopIteration, KeyError):
        results["sublane_list"] = False

    return results
