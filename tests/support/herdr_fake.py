"""Single shared stateful fake herdr CLI (Redmine #13407, US A of #13398).

Parent Feature #12531 ``120_シナリオ・受入テスト基盤``. Design source of truth:
``vibes/docs/logics/herdr-scenario-test-foundation.md`` (#13398, closed) §1.1 /
§2.1 / §2.3, auditor arbitration #13398 j#73769 (裁定 1 = the fake's primary
boundary is the **outermost subprocess ``Runner`` boundary**).

Why this module exists
----------------------
Today's herdr test doubles are inline per-file fakes (``_StatefulHerdr`` /
``RecordingRunner`` / ``FakePort`` / ``_CloseHerdr`` / …) redefined across 50+
modules; none of them share a workspace↔pane↔agent state machine, so the herdr
0.7.1 lifecycle contracts (pane-close→workspace auto-vanish, ``--workspace``
prefix placement, ``agent list`` decode, ``wait`` change-semantics) drift
independently and no single canonical model exists (design §1.2). This module is
that single canonical model: a stateful fake injected at the **same ``Runner``
port the real code uses** (:data:`~...infrastructure.herdr_transport.Runner` =
``Callable[argv, ...] -> CompletedProcess``), so the subject-under-test drives the
real ``HerdrCliTransport`` / ``herdr_session_start`` / route authority and only
the outermost ``herdr`` subprocess is replaced (Detroit-school: keep every real
collaborator).

Contract faithfulness (design §1.1, modelled faces A–F)
------------------------------------------------------
- **A ``agent start``** — parses ``agent start <NAME> [--cwd] [--workspace <id>]
  [--env K=V]... [--no-focus] [--permission-mode M] -- <provider> [argv…]``,
  applies ``NAME`` directly (``result.agent.name == NAME``), mints a live locator
  ``<workspace>:p<n>`` inside the requested ``--workspace``, and returns the single
  ``type: agent_started`` envelope whose locator is ``result.agent.pane_id``.
- **B ``agent list``** — renders the live inventory from state; each row carries
  the durable ``name`` and the transient locator under ``pane_id`` (alias
  ``pane`` / ``location`` selectable, matching the real decode aliases).
- **C ``agent get`` / name persistence** — a started agent's assigned name is
  stable; ``agent get`` nests the status token under ``result.agent`` (live 0.7.1
  shape). The locator is transient (re-minted per launch), never the identity.
- **D ``workspace create``** — mints a fresh ``w<n>`` workspace born with exactly
  one empty base ``root_pane`` (``pane_count: 1``), reproducing the cold-start base
  pane #13330 reclaims.
- **E ``pane close``** — removes the pane and, when its workspace has **zero**
  panes left, auto-closes the workspace (live-measured #13380: a lane-zero host
  workspace has no husk). Symmetrically, when a **tab** has zero panes left the
  tab auto-vanishes (live-measured #13411 j#73668), the tab analogue of E.
- **G ``tab create`` / ``agent start --tab [--split right]``** (Redmine #13411) —
  ``tab create --workspace <id>`` mints a fresh ``<id>:t<n>`` tab born with one
  empty ``root_pane`` (the tab analogue of D), returned in a ``tab_created``
  envelope. ``agent start --tab <tab_id>`` places the launched pane in that tab;
  ``--split right`` places it beside the tab's live pane. Each ``agent list`` row
  carries its ``tab_id`` (real 0.7.1 rows expose it alongside ``workspace_id``),
  so a heal reads the live slot's tab to rejoin it.
- **F ``wait agent-status``** — **change-semantics** (PoC E9): a wait returns only
  on a *change into* the requested status; already being in it does **not** return
  (it times out). Modelled deterministically with **no real time** — a pre-armed
  logical transition (:meth:`FakeHerdr.arm_transition`) is what a wait returns on;
  an unarmed wait times out; a wait on an absent target reports absent.

Fail-closed posture (design §2.3, no silent success)
----------------------------------------------------
- An **unknown / unmodelled argv** never returns a canned success: it raises
  :class:`UnknownHerdrCommandError`. Silent success is the worst false confidence,
  so the fake's default for an argv it does not model is to fail loudly — that is
  the *production-seam → fake* drift signal (a new API face used before the fake
  models it surfaces immediately).
- The fake carries **no** fail-closed *vocabulary* of its own. Injection hooks
  (:meth:`misplace_next_launch`, :meth:`drop_next_locator`, ``extra_list_rows``,
  duplicate names, :meth:`arm_timeout`) reproduce the *stimulus* — a mislocated
  launch, a blank locator, a malformed row, a duplicate name, a wait timeout — and
  the **real code** renders the ``no_match`` / ``missing_locator`` /
  ``target_unavailable`` verdict. The fake supplies shapes, not judgements.

No live binary, no tmux, no clock: identities are abstract placeholders only (no
home paths / secret-shaped literals), matching the public/private boundary of the
sibling ``support.delegation_route_fakes``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Self-bootstrap the repo-local ``src`` so single-file / isolated discovery works
# regardless of install state (the #12490 idiom shared with the sibling support
# module); harmless when ``src`` is already importable. The fake needs no ``src``
# import itself — it deals only in JSON shapes — but a scenario that imports only
# this module still gets the package path it will drive.
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# -- herdr status tokens (the real 0.7.1 vocabulary the wait/get faces speak) ----
#: Runtime status tokens a herdr agent reports (PoC E9 / E12–E14). These are the
#: raw herdr tokens, not mozyo runtime states — the real ``agent_state`` reader
#: maps them; the fake only echoes whichever the state machine holds.
STATUS_IDLE = "idle"
STATUS_WORKING = "working"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"

#: Default status a freshly launched agent snapshots at (booted but not yet
#: driven): idle. A ``wait`` for a *change into* another status is what advances
#: it (change-semantics), never the mere fact of being started.
DEFAULT_START_STATUS = STATUS_IDLE

#: The real herdr row locator key and its two accepted aliases (mirrors
#: ``herdr_identity.AGENT_KEY_LOCATOR`` + aliases so a scenario can exercise the
#: alias decode paths through the fake).
LOCATOR_KEY = "pane_id"
LOCATOR_ALIASES = ("pane", "location")

#: The exact substring the real change-semantics wait emits on timeout (PoC E9
#: c2). The turn-start rail classifies a wait by matching timeout/absent tokens in
#: stderr, so the fake must speak the same words for that classification to hold.
WAIT_TIMEOUT_MESSAGE = "timed out waiting for agent status change"
#: The substring an absent-target wait emits (PoC E9 c3: a wait on a missing pane
#: fails as a pane-get error). Contains ``no such pane`` so the rail classifies it
#: absent, distinct from a timeout.
WAIT_ABSENT_MESSAGE = "no such pane: pane get failed"


class UnknownHerdrCommandError(AssertionError):
    """Raised when the fake is driven with an argv it does not model.

    Inherits :class:`AssertionError` so an unmodelled command surfaces as a test
    failure (never a silent canned success — design §2.3). It is the *fake ←
    production seam drift* signal: production reaching for an herdr surface the
    fake has not taught means the scenario is no longer faithful and must fail
    loudly rather than pass on a fabricated success.
    """


@dataclass
class _Workspace:
    """One herdr terminal workspace: its live pane ids (root pane included).

    ``pane_tab`` maps each live pane to the tab it lives in (Redmine #13411);
    the workspace base pane and any non-tab pane map to ``""`` (the default tab).
    ``tab_seq`` is the monotonic per-workspace tab counter (never reused).
    """

    workspace_id: str
    panes: list = field(default_factory=list)  # pane ids, in creation order
    cwd: str = ""
    pane_seq: int = 0  # monotonic per-workspace pane counter (never reused)
    pane_tab: dict = field(default_factory=dict)  # pane_id -> tab_id ("" = default)
    tab_seq: int = 0  # monotonic per-workspace tab counter (never reused)


@dataclass
class _Agent:
    """One launched agent: durable name + transient locator + snapshot status."""

    name: str
    pane_id: str
    workspace_id: str
    provider: str = ""
    cwd: str = ""
    status: str = DEFAULT_START_STATUS
    tab_id: str = ""  # the herdr tab the agent's pane lives in (Redmine #13411)
    launch_argv: list = field(default_factory=list)  # the post-``--`` provider argv
    env: dict = field(default_factory=dict)  # the injected ``--env K=V`` pairs


class FakeHerdr:
    """A single shared, stateful fake herdr CLI over the ``Runner`` boundary.

    Inject :meth:`run` where the real code takes a ``runner`` (the
    :data:`~...infrastructure.herdr_transport.Runner` port) and :meth:`popen`
    where it takes a ``popen`` factory (the turn-start wait rail). The in-memory
    state machine holds workspaces, their panes, and the launched agents; the
    modelled faces (A–F, see the module docstring) read and mutate it.

    Every driven call is recorded on :attr:`calls` (the argv **after** the binary)
    so a scenario can assert *what herdr was asked to do and on which rail* — the
    routing-observation posture of ``support.delegation_route_fakes`` (design
    §3.4). All state is public via read helpers so a unit can pin transitions
    directly.
    """

    def __init__(self, *, read_text: str = "codex composer rendered") -> None:
        self._workspaces: dict = {}
        self._agents: dict = {}  # pane_id -> _Agent
        self._workspace_seq = 0
        #: Rendered pane text served by ``agent read`` (set to ``""`` to simulate a
        #: live-but-still-booting TUI, #13378).
        self.read_text = read_text
        #: Every driven argv tail (after the binary), in order — the routing tape.
        self.calls: list = []
        # --- one-shot fail-closed injection state (design §2.1 stimuli) ---------
        self._misplace_next: Optional[str] = None
        self._drop_next_locator = False
        #: Extra rows spliced verbatim into the next ``agent list`` payload (a
        #: malformed-row / recognised-empty injection face). Cleared after use.
        self.extra_list_rows: list = []
        #: Which key an ``agent list`` row renders its locator under (``pane_id``
        #: by default; set to an alias to exercise the alias decode path).
        self.locator_render_key = LOCATOR_KEY
        # --- pre-armed wait transitions (change-semantics, design §1.1 F) -------
        self._armed_transitions: list = []  # (target, to_status) queued FIFO

    # -- seeding / injection --------------------------------------------------

    def seed_workspace(self, *, cwd: str = "") -> str:
        """Mint a workspace directly (test seeding), returning its id.

        The programmatic twin of a ``workspace create`` call for a scenario that
        needs pre-existing herdr state before it drives the real seam.
        """
        return self._mint_workspace(cwd=cwd).workspace_id

    def seed_agent(
        self,
        name: str,
        *,
        workspace_id: str,
        provider: str = "",
        status: str = DEFAULT_START_STATUS,
        cwd: str = "",
    ) -> str:
        """Place a live agent directly in ``workspace_id``, returning its locator.

        Seeds the live inventory the way a prior ``agent start`` would have, so a
        scenario can stand up an existing lane slot without replaying its launch.
        """
        ws = self._workspaces.get(workspace_id)
        if ws is None:
            raise UnknownHerdrCommandError(
                f"seed_agent: unknown workspace {workspace_id!r}"
            )
        pane_id = self._mint_pane(ws)
        self._agents[pane_id] = _Agent(
            name=name,
            pane_id=pane_id,
            workspace_id=workspace_id,
            provider=provider,
            status=status,
            cwd=cwd,
        )
        return pane_id

    def misplace_next_launch(self, workspace_id: str) -> None:
        """Make the next ``agent start`` return a locator in ``workspace_id``.

        Reproduces herdr ignoring ``--workspace`` (spec drift) so the *real*
        session-start code sees a launch that landed outside the requested
        workspace and fails closed (the #13330 review j#73231 guard). The fake
        renders the mislocated locator; it renders no verdict.
        """
        self._misplace_next = workspace_id

    def drop_next_locator(self) -> None:
        """Make the next ``agent start`` return a blank ``pane_id``.

        Reproduces a launch envelope with no usable locator so the real code
        fails closed (``refuse to return a blank handle``) rather than the fake
        deciding the outcome.
        """
        self._drop_next_locator = True

    def arm_transition(self, target: str, to_status: str) -> None:
        """Pre-arm a status *change into* ``to_status`` for ``target`` (FIFO).

        Change-semantics (PoC E9 / E12): a ``wait`` returns only on a transition.
        Arming one before the wait is exactly the "arm → inject → collect" order
        the real turn-start rail contracts (E12); the fake fires it with no real
        time. An unarmed wait times out; a wait whose target is absent reports
        absent.
        """
        self._armed_transitions.append((target, to_status))

    # -- Runner port (subprocess.run shape) -----------------------------------

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **_):
        """The injected ``Runner``: dispatch one herdr subprocess call.

        ``argv[0]`` is the (fake) binary path; ``argv[1:]`` is the herdr subcommand
        the real code built. Returns a :class:`subprocess.CompletedProcess` with a
        JSON ``stdout`` (success) or a non-empty ``stderr`` + non-zero exit
        (failure), never raising for a *modelled* command. An **unmodelled** argv
        raises :class:`UnknownHerdrCommandError` (fail-closed, design §2.3).
        """
        rest = list(argv[1:])
        self.calls.append(rest)
        head = rest[:2]
        if head == ["workspace", "create"]:
            return self._cmd_workspace_create(argv, rest)
        if head == ["tab", "create"]:
            return self._cmd_tab_create(argv, rest)
        if head == ["agent", "start"]:
            return self._cmd_agent_start(argv, rest)
        if head == ["agent", "list"]:
            return self._cmd_agent_list(argv)
        if head == ["agent", "get"]:
            return self._cmd_agent_get(argv, rest)
        if head == ["agent", "read"]:
            return self._cmd_agent_read(argv, rest)
        if head == ["pane", "close"]:
            return self._cmd_pane_close(argv, rest)
        if head in (["pane", "send-text"], ["pane", "send-keys"]):
            return self._cmd_pane_send(argv, rest)
        raise UnknownHerdrCommandError(f"unmodelled herdr call: {list(argv)!r}")

    # -- PopenFactory (subprocess.Popen shape) for the wait rail --------------

    def popen(self, argv, stdout=None, stderr=None, text=None, **_):
        """The injected wait ``popen``: model ``wait agent-status`` (change-semantics).

        Only ``wait agent-status <target> --status <s> --timeout <ms>`` is
        modelled (the sole command the turn-start rail arms). The outcome is
        resolved **immediately and deterministically** (no real time): a pre-armed
        transition into ``<s>`` for ``<target>`` fires the transition and returns
        the event (exit 0); an absent target reports absent; otherwise it times out
        (change-semantics — already being in ``<s>`` does not return). Any other
        popen argv is fail-closed.
        """
        rest = list(argv[1:])
        self.calls.append(rest)
        if rest[:2] != ["wait", "agent-status"]:
            raise UnknownHerdrCommandError(f"unmodelled herdr popen: {list(argv)!r}")
        target = rest[2] if len(rest) > 2 else ""
        want = _flag_value(rest, "--status")
        return self._resolve_wait(target, want)

    # -- command handlers -----------------------------------------------------

    def _cmd_workspace_create(self, argv, rest):
        ws = self._mint_workspace(cwd=_flag_value(rest, "--cwd") or "")
        root_pane = ws.panes[0]
        return _ok(
            argv,
            {
                "result": {
                    "type": "workspace_created",
                    "workspace": {"workspace_id": ws.workspace_id},
                    "root_pane": {"pane_id": root_pane},
                    "pane_count": 1,
                }
            },
        )

    def _cmd_tab_create(self, argv, rest):
        # G (Redmine #13411): mint a fresh tab in an existing workspace, born with
        # one empty root pane (the tab analogue of `workspace create`). Fails closed
        # on an unknown workspace so the real code sees a create failure, never a
        # fabricated tab id.
        wid = _flag_value(rest, "--workspace")
        ws = self._workspaces.get(wid)
        if ws is None:
            return _err(argv, f"unknown workspace: {wid}")
        ws.tab_seq += 1
        tab_id = f"{wid}:t{ws.tab_seq}"
        root_pane = self._mint_pane(ws)
        ws.pane_tab[root_pane] = tab_id
        return _ok(
            argv,
            {
                "result": {
                    "type": "tab_created",
                    "tab": {"tab_id": tab_id},
                    "root_pane": {"pane_id": root_pane},
                }
            },
        )

    def _cmd_agent_start(self, argv, rest):
        parsed = _parse_agent_start(rest)
        # Resolve (or auto-create) the target workspace. Real herdr with no
        # ``--workspace`` auto-creates one (the empty-base-pane source, #13330);
        # with ``--workspace`` it lands in that existing workspace.
        if parsed.workspace_id:
            ws = self._workspaces.get(parsed.workspace_id)
            if ws is None:
                return _err(argv, f"unknown workspace: {parsed.workspace_id}")
        else:
            ws = self._mint_workspace(cwd=parsed.cwd)
        # A `--tab` must name a live tab in the workspace (its root pane exists at
        # launch time, before the reclaim). Fail closed on an unknown tab so the
        # real code sees a launch failure rather than a fabricated placement (#13411).
        if parsed.tab_id and parsed.tab_id not in set(ws.pane_tab.values()):
            return _err(argv, f"unknown tab: {parsed.tab_id}")
        pane_id = self._mint_pane(ws)
        if parsed.tab_id:
            ws.pane_tab[pane_id] = parsed.tab_id
        self._agents[pane_id] = _Agent(
            name=parsed.name,
            pane_id=pane_id,
            workspace_id=ws.workspace_id,
            provider=parsed.provider,
            cwd=parsed.cwd,
            tab_id=parsed.tab_id,
            launch_argv=parsed.launch_argv,
            env=parsed.env,
        )
        # Fail-closed injection faces (the fake supplies the stimulus shape; the
        # real code renders the verdict).
        rendered_locator = pane_id
        if self._drop_next_locator:
            self._drop_next_locator = False
            rendered_locator = ""
        elif self._misplace_next is not None:
            rendered_locator = f"{self._misplace_next}:p1"
            self._misplace_next = None
        return _ok(
            argv,
            {
                "id": "cli:agent:start",
                "result": {
                    "agent": {
                        "name": parsed.name,
                        "pane_id": rendered_locator,
                        "argv": parsed.launch_argv,
                    },
                    "type": "agent_started",
                },
            },
        )

    def _cmd_agent_list(self, argv):
        rows = []
        for agent in self._agents.values():
            row = {"name": agent.name, "agent_status": agent.status}
            row[self.locator_render_key] = agent.pane_id
            # Real 0.7.1 rows carry the slot's tab (#13411); render it when the
            # agent lives in one so a heal can rejoin the same tab.
            if agent.tab_id:
                row["tab_id"] = agent.tab_id
            rows.append(row)
        # Splice any injected malformed / extra rows verbatim (one-shot).
        if self.extra_list_rows:
            rows = rows + list(self.extra_list_rows)
            self.extra_list_rows = []
        return _ok(argv, {"agents": rows})

    def _cmd_agent_get(self, argv, rest):
        target = rest[2] if len(rest) > 2 else ""
        agent = self._resolve_agent(target)
        if agent is None:
            return _err(argv, f"agent not found: {target}")
        return _ok(
            argv,
            {
                "id": "cli:agent:get",
                "result": {
                    "agent": {
                        "name": agent.name,
                        "pane_id": agent.pane_id,
                        "agent_status": agent.status,
                    }
                },
            },
        )

    def _cmd_agent_read(self, argv, rest):
        target = rest[2] if len(rest) > 2 else ""
        agent = self._resolve_agent(target)
        if agent is None:
            return _err(argv, f"agent_not_found: {target}")
        return _ok(
            argv,
            {"result": {"read": {"text": self.read_text, "truncated": False}}},
        )

    def _cmd_pane_close(self, argv, rest):
        pane_id = rest[2] if len(rest) > 2 else ""
        ws = self._workspace_of_pane(pane_id)
        if ws is None:
            return _err(argv, f"no such pane: {pane_id}")
        ws.panes.remove(pane_id)
        # E (tab axis, #13411): the pane leaves its tab; the tab lives on only while
        # another pane still references it. `pane_tab` is the sole tab registry, so a
        # tab with no remaining panes simply stops being referenced (auto-vanish).
        ws.pane_tab.pop(pane_id, None)
        self._agents.pop(pane_id, None)
        # E: the last pane closing auto-vanishes the workspace (no husk, #13380).
        if not ws.panes:
            del self._workspaces[ws.workspace_id]
        return _ok(argv, {"result": {"type": "ok"}})

    def _cmd_pane_send(self, argv, rest):
        target = rest[2] if len(rest) > 2 else ""
        # A send lands only in a live pane; an unknown target fails closed so the
        # real transport reports the send failure rather than a fabricated OK.
        if self._workspace_of_pane(target) is None:
            return _err(argv, f"no such pane: {target}")
        return _ok(argv, {"result": {"type": "ok"}})

    # -- wait resolution (change-semantics, no real time) ---------------------

    def _resolve_wait(self, target: str, want_status: str):
        if self._resolve_agent(target) is None:
            return _FakeWaitProcess(returncode=1, stderr=WAIT_ABSENT_MESSAGE)
        for index, (armed_target, to_status) in enumerate(self._armed_transitions):
            if armed_target == target and to_status == want_status:
                del self._armed_transitions[index]
                agent = self._resolve_agent(target)
                if agent is not None:
                    agent.status = to_status  # the transition actually happens
                event = {"event": "pane.agent_status_changed", "status": to_status}
                return _FakeWaitProcess(returncode=0, stdout=json.dumps(event))
        # No armed change into ``want_status`` -> change-semantics timeout, even if
        # the agent is *already* in ``want_status`` (PoC E9 c2).
        return _FakeWaitProcess(returncode=1, stderr=WAIT_TIMEOUT_MESSAGE)

    # -- state helpers (public read model) ------------------------------------

    def _mint_workspace(self, *, cwd: str = "") -> _Workspace:
        self._workspace_seq += 1
        wid = f"w{self._workspace_seq}"
        ws = _Workspace(workspace_id=wid, cwd=cwd)
        # Born with exactly one empty base pane (pane_count: 1), #13330.
        ws.pane_seq = 1
        ws.panes.append(f"{wid}:p1")
        self._workspaces[wid] = ws
        return ws

    def _mint_pane(self, ws: _Workspace) -> str:
        ws.pane_seq += 1
        pane_id = f"{ws.workspace_id}:p{ws.pane_seq}"
        ws.panes.append(pane_id)
        return pane_id

    def _workspace_of_pane(self, pane_id: str) -> Optional[_Workspace]:
        for ws in self._workspaces.values():
            if pane_id in ws.panes:
                return ws
        return None

    def _resolve_agent(self, target: str) -> Optional[_Agent]:
        """Resolve an agent by its transient locator or its durable name."""
        if not target:
            return None
        agent = self._agents.get(target)
        if agent is not None:
            return agent
        for candidate in self._agents.values():
            if candidate.name == target:
                return candidate
        return None

    # -- observation helpers (for direct unit pins) ---------------------------

    @property
    def workspace_ids(self) -> list:
        """The ids of the currently live workspaces (creation order)."""
        return list(self._workspaces)

    def panes_of(self, workspace_id: str) -> list:
        """The live pane ids of ``workspace_id`` (``[]`` if it has vanished)."""
        ws = self._workspaces.get(workspace_id)
        return list(ws.panes) if ws else []

    def tab_ids(self, workspace_id: str) -> list:
        """The distinct live tab ids in ``workspace_id`` (creation order, #13411).

        A tab is live only while a pane still references it, so a tab whose panes
        all closed has auto-vanished from this list (the tab-axis auto-vanish, E).
        """
        ws = self._workspaces.get(workspace_id)
        if ws is None:
            return []
        seen: list = []
        for pane in ws.panes:
            tab = ws.pane_tab.get(pane, "")
            if tab and tab not in seen:
                seen.append(tab)
        return seen

    def tab_of(self, pane_id: str) -> str:
        """The tab id a live pane lives in (``""`` for a default-tab / absent pane)."""
        for ws in self._workspaces.values():
            if pane_id in ws.pane_tab:
                return ws.pane_tab[pane_id]
        return ""

    @property
    def agents(self) -> list:
        """The live agents as ``{"name", "pane_id", "status"}`` dicts (list order)."""
        return [
            {"name": a.name, "pane_id": a.pane_id, "status": a.status}
            for a in self._agents.values()
        ]

    def agent_named(self, name: str) -> Optional[dict]:
        """The single live agent carrying ``name``, or ``None`` (fail on duplicate)."""
        matches = [a for a in self._agents.values() if a.name == name]
        if len(matches) > 1:
            raise UnknownHerdrCommandError(
                f"agent_named({name!r}): {len(matches)} live agents share the name"
            )
        if not matches:
            return None
        a = matches[0]
        return {"name": a.name, "pane_id": a.pane_id, "status": a.status}

    @property
    def start_argvs(self) -> list:
        """Every ``agent start`` argv tail driven so far (launch-argv assertions)."""
        return [c for c in self.calls if c[:2] == ["agent", "start"]]


class _FakeWaitProcess:
    """A deterministic stand-in for a ``wait agent-status`` ``Popen`` (no real time).

    Exposes the slice of the ``Popen`` surface the turn-start rail collects
    against: :meth:`communicate` (returns ``(stdout, stderr)`` immediately),
    :attr:`returncode`, and a no-op :meth:`kill`. The outcome is fixed at
    construction, so ``communicate`` never blocks and no ``timeout`` ever elapses.
    """

    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover - the rail only calls this on reap
        return None

    def poll(self):
        return self.returncode


# -- module-level parsing / envelope helpers ----------------------------------


@dataclass
class _StartArgs:
    name: str
    workspace_id: str = ""
    tab_id: str = ""
    split: str = ""
    cwd: str = ""
    provider: str = ""
    no_focus: bool = False
    permission_mode: str = ""
    env: dict = field(default_factory=dict)
    launch_argv: list = field(default_factory=list)


def _parse_agent_start(rest: list) -> _StartArgs:
    """Parse an ``agent start`` argv tail; fail-closed on an unmodelled flag.

    ``rest`` is ``["agent", "start", NAME, <flags…>, "--", provider, argv…]``. A
    missing NAME or an unknown flag before ``--`` raises
    :class:`UnknownHerdrCommandError` (the fake never guesses an unmodelled launch
    shape).
    """
    if len(rest) < 3 or rest[2].startswith("--"):
        raise UnknownHerdrCommandError(
            f"agent start requires a NAME positional: {rest!r}"
        )
    args = _StartArgs(name=rest[2])
    i = 3
    n = len(rest)
    while i < n:
        token = rest[i]
        if token == "--":
            tail = rest[i + 1 :]
            args.provider = tail[0] if tail else ""
            args.launch_argv = tail
            return args
        if token == "--workspace":
            args.workspace_id = rest[i + 1]
            i += 2
        elif token == "--tab":
            args.tab_id = rest[i + 1]
            i += 2
        elif token == "--split":
            args.split = rest[i + 1]
            i += 2
        elif token == "--cwd":
            args.cwd = rest[i + 1]
            i += 2
        elif token == "--permission-mode":
            args.permission_mode = rest[i + 1]
            i += 2
        elif token == "--env":
            key, _, value = rest[i + 1].partition("=")
            args.env[key] = value
            i += 2
        elif token == "--no-focus":
            args.no_focus = True
            i += 1
        else:
            raise UnknownHerdrCommandError(
                f"agent start: unmodelled flag {token!r} in {rest!r}"
            )
    # No ``--`` separator: a launch with no provider argv is not a shape real
    # herdr emits, so fail closed rather than invent one.
    raise UnknownHerdrCommandError(f"agent start missing '--' provider separator: {rest!r}")


def _flag_value(rest: list, flag: str) -> str:
    """The value following ``flag`` in ``rest`` (``""`` if the flag is absent)."""
    try:
        return rest[rest.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def _ok(argv, payload) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(
        list(argv), 0, stdout=json.dumps(payload), stderr=""
    )


def _err(argv, message: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr=message)


__all__ = (
    "DEFAULT_START_STATUS",
    "FakeHerdr",
    "LOCATOR_ALIASES",
    "LOCATOR_KEY",
    "STATUS_BLOCKED",
    "STATUS_DONE",
    "STATUS_IDLE",
    "STATUS_WORKING",
    "UnknownHerdrCommandError",
    "WAIT_ABSENT_MESSAGE",
    "WAIT_TIMEOUT_MESSAGE",
)
