"""A nested-split herdr backend for geometry tests (Redmine #14996 R2).

The shared :mod:`tests.support.herdr_fake` models a container as ONE split with
one ratio, which is exactly why the project-column defect (#14996 j#99833) was
invisible to the suite: the L only exists in a *nested* split tree, and a
single-divider model cannot express one (j#99845: "現行testはworkspace/tab/split
argvのみを検証し、nested geometryを再現・検証していなかった").

This module models the tree itself, with the semantics measured on herdr 0.7.4 in
a disposable instance:

- ``pane split`` / ``agent start --split`` subdivide a LEAF: the split target
  becomes the first child of a new split node and the fresh pane the second. A
  launch with no ``--split`` still subdivides the ACTIVE pane, using herdr's own
  default direction (``right``) — the step that turned an appended coordinator
  pair into the observed L.
- closing a pane collapses its parent split, promoting the sibling.
- ``pane move --new-tab`` detaches a pane into a tab of its own; a tab whose last
  pane leaves is auto-closed.
- ``pane move --tab <t> --split <d> --target-pane <p>`` re-inserts against an
  explicit target, subdividing that leaf exactly like a split does. Without
  ``--target-pane`` it subdivides the tab's focused pane instead — measured, and
  the reason the fix cannot rely on that form.
- rects are derived from the tree at read time (herdr stores ratios and re-derives
  geometry), with a ``down`` split of 23 rows rendering 12/11 and a ``right``
  split of 54 columns rendering 27/27 — the measured rounding.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    decode_assigned_name,
)
from .herdr_fake import apply_resize_amount


@dataclass
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class Leaf:
    pane_id: str


@dataclass
class Split:
    direction: str
    ratio: float
    first: object
    second: object


def _first_extent(extent: int, ratio: float) -> int:
    """The first child's cell extent — the SAME arithmetic the flat pair renderer uses.

    ``round(extent * ratio)`` is what ``support.herdr_fake.render_pane_layout`` applies
    and what herdr rendered live (23 rows at 0.5 -> 12/11, 54 columns at 0.5 -> 27/27), so
    a one-split tree and the flat pair renderer describe the same geometry rather than two
    fakes drifting apart. ``tests/unit/.../test_herdr_pane_tree_parity`` pins that.
    """
    return round(extent * ratio)


@dataclass
class Tab:
    tab_id: str
    workspace_id: str
    root: Optional[object] = None
    focused: str = ""
    bounds: Rect = field(default_factory=lambda: Rect(0, 0, 54, 23))

    # -- tree surgery -----------------------------------------------------
    def panes(self) -> list:
        found: list = []

        def walk(node) -> None:
            if isinstance(node, Leaf):
                found.append(node.pane_id)
            elif isinstance(node, Split):
                walk(node.first)
                walk(node.second)

        walk(self.root)
        return found

    def _replace(self, node, pane_id: str, replacement):
        if isinstance(node, Leaf):
            return replacement if node.pane_id == pane_id else node
        if isinstance(node, Split):
            node.first = self._replace(node.first, pane_id, replacement)
            node.second = self._replace(node.second, pane_id, replacement)
        return node

    def subdivide(self, target: str, direction: str, pane_id: str) -> bool:
        """Turn the ``target`` leaf into ``Split(direction, target, pane_id)``."""
        if target not in self.panes():
            return False
        self.root = self._replace(
            self.root, target, Split(direction, 0.5, Leaf(target), Leaf(pane_id))
        )
        return True

    def remove(self, pane_id: str) -> bool:
        """Drop a pane, collapsing its parent split onto the surviving sibling."""
        if pane_id not in self.panes():
            return False

        def prune(node):
            if isinstance(node, Leaf):
                return None if node.pane_id == pane_id else node
            first, second = prune(node.first), prune(node.second)
            if first is None:
                return second
            if second is None:
                return first
            node.first, node.second = first, second
            return node

        self.root = prune(self.root)
        if self.focused == pane_id:
            remaining = self.panes()
            self.focused = remaining[0] if remaining else ""
        return True

    def resize(self, pane_id: str, direction: str, amount: float) -> bool:
        """Resize the nearest ancestor divider whose axis matches ``direction``."""
        axis = "down" if direction in {"up", "down"} else "right"

        def locate(node):
            if isinstance(node, Leaf):
                return (node.pane_id == pane_id, None)
            first_has, first_split = locate(node.first)
            second_has, second_split = locate(node.second)
            contains = first_has or second_has
            nearest = first_split or second_split
            if contains and nearest is None and node.direction == axis:
                nearest = node
            return contains, nearest

        contains, split = locate(self.root)
        if not contains or split is None:
            return False
        split.ratio = apply_resize_amount(split.ratio, direction, amount)
        return True

    # -- read model -------------------------------------------------------
    def layout_payload(self) -> dict:
        panes: list = []
        splits: list = []

        def walk(node, rect: Rect, path: str, depth: int) -> None:
            if isinstance(node, Leaf):
                panes.append(
                    {
                        "pane_id": node.pane_id,
                        "rect": {
                            "x": rect.x, "y": rect.y,
                            "width": rect.width, "height": rect.height,
                        },
                    }
                )
                return
            splits.append(
                {
                    "id": f"split_{depth}_{path or 'root'}",
                    "direction": node.direction,
                    "ratio": node.ratio,
                    "rect": {
                        "x": rect.x, "y": rect.y,
                        "width": rect.width, "height": rect.height,
                    },
                }
            )
            if node.direction == "down":
                head = _first_extent(rect.height, node.ratio)
                walk(node.first, Rect(rect.x, rect.y, rect.width, head), path + "0", depth + 1)
                walk(
                    node.second,
                    Rect(rect.x, rect.y + head, rect.width, rect.height - head),
                    path + "1", depth + 1,
                )
            else:
                head = _first_extent(rect.width, node.ratio)
                walk(node.first, Rect(rect.x, rect.y, head, rect.height), path + "0", depth + 1)
                walk(
                    node.second,
                    Rect(rect.x + head, rect.y, rect.width - head, rect.height),
                    path + "1", depth + 1,
                )

        if self.root is not None:
            walk(self.root, self.bounds, "", 0)
        return {
            "result": {
                "type": "pane_layout",
                "layout": {
                    "tab_id": self.tab_id,
                    "workspace_id": self.workspace_id,
                    "panes": panes,
                    "splits": splits,
                },
            }
        }


class PaneTreeHerdr:
    """A ``runner``-shaped herdr backed by real nested split trees.

    Answers exactly the commands the project-column rail issues — ``agent list``,
    ``pane layout``, ``pane move`` — plus the ``pane split`` used to build a
    scenario. Anything else raises, so a test cannot pass by silently no-opping a
    command the production code actually depends on.
    """

    def __init__(self, workspace_id: str = "w1") -> None:
        self.workspace_id = workspace_id
        self.tabs: dict = {}
        self.agents: dict = {}  # pane_id -> assigned name
        self._pane_seq = 0
        self._tab_seq = 0
        self.calls: list = []
        #: Panes whose next ``pane move`` herdr should refuse (fault injection).
        self.move_refusals: set = set()
        #: Refuse every ``pane move`` from this 1-based attempt on — the shape that
        #: strands a pane: a detach succeeds, the next step dies, and the recovery
        #: move dies too. A per-pane refusal cannot produce it, because the pane the
        #: recovery would move is by construction one that moved successfully.
        self.refuse_from_move: Optional[int] = None
        #: Panes whose next ``pane move`` should report ``changed:false``.
        self.move_unchanged: set = set()
        #: Rename a pane's assigned name at this point in the sequence, to prove the
        #: closing identity check is real rather than decorative.
        self.rename_after_moves: dict = {}
        #: Refuse or accept-without-changing ``pane resize`` for fault injection.
        self.resize_refused = False
        self.resize_unchanged = False
        self.resizes: list = []
        #: Panes whose ``agent list`` row is shell residue — the durable identity is
        #: there, the managed agent is not. Rendered as a present-but-blank ``agent``
        #: field, which is the positive stale signal ``classify_named_slot`` reads.
        self.stale_panes: set = set()
        #: ``{mozyo workspace_id: path}`` rendered as each pane's ``foreground_cwd``.
        #: Real rows carry it (#13806) and the project-column authority reads it, so a
        #: fake that omitted it would let a test pass a check production cannot.
        self.cwd_by_workspace: dict = {}
        #: ``{pane_id: provider}`` overriding the detected agent a row reports, so a
        #: test can express a pane whose live provider contradicts its assigned name
        #: (#14996 R2 review j#99913 finding_2) or one herdr does not recognise.
        self.detected_override: dict = {}
        #: Extra ``agent list`` rows spliced in verbatim — the way a test expresses a
        #: row the tree model itself cannot hold (a pane with no usable locator, a
        #: contradictory workspace, a payload that is not a mapping).
        self.extra_rows: list = []
        self._moves = 0
        self._move_attempts = 0

    # -- scenario building ------------------------------------------------
    def new_tab(self) -> Tab:
        self._tab_seq += 1
        tab = Tab(tab_id=f"{self.workspace_id}:t{self._tab_seq}", workspace_id=self.workspace_id)
        self.tabs[tab.tab_id] = tab
        return tab

    def _mint_pane(self) -> str:
        self._pane_seq += 1
        return f"{self.workspace_id}:p{self._pane_seq}"

    def seed_pane(self, tab: Tab, assigned_name: str = "") -> str:
        """The tab's very first pane (herdr's tab root)."""
        pane_id = self._mint_pane()
        tab.root = Leaf(pane_id)
        tab.focused = pane_id
        if assigned_name:
            self.agents[pane_id] = assigned_name
        return pane_id

    def split_pane(
        self, tab: Tab, target: str, direction: str, assigned_name: str = "",
        focus: bool = False,
    ) -> str:
        pane_id = self._mint_pane()
        if not tab.subdivide(target, direction, pane_id):
            raise AssertionError(f"cannot split unknown pane {target!r}")
        if assigned_name:
            self.agents[pane_id] = assigned_name
        if focus:
            tab.focused = pane_id
        return pane_id

    def seed_columns(self, tab: Tab, columns: "list") -> list:
        """Build a tab that is ALREADY project-columnar — one column per entry.

        ``columns`` is a list of assigned-name lists, each becoming a ``down``-split
        column, the columns nested left-to-right under ``right`` splits. This is
        the state a tab is in after an earlier append reflowed, so a test can start
        from it instead of replaying every prior launch.
        """
        built: list = []
        nodes: list = []
        for names in columns:
            panes = [self._mint_pane() for _ in names]
            for pane_id, name in zip(panes, names):
                self.agents[pane_id] = name
            node = Leaf(panes[-1])
            for pane_id in reversed(panes[:-1]):
                node = Split("down", 0.5, Leaf(pane_id), node)
            nodes.append(node)
            built.append(panes)
        root = nodes[-1]
        for node in reversed(nodes[:-1]):
            root = Split("right", 0.5, node, root)
        tab.root = root
        tab.focused = built[0][0]
        return built

    def launch_into(
        self, tab: Tab, assigned_name: str, *, split: str = "", focus: bool = False
    ) -> str:
        """Reproduce ``agent start`` verbatim: subdivide the ACTIVE pane.

        A launch with no ``--split`` still subdivides — herdr applies its own
        default direction (``right``). That is the exact step the appended
        coordinator pair took when it produced the L.
        """
        return self.split_pane(
            tab, tab.focused, split or "right", assigned_name=assigned_name, focus=focus
        )

    def tab_of(self, pane_id: str) -> Optional[Tab]:
        for tab in self.tabs.values():
            if pane_id in tab.panes():
                return tab
        return None

    def rects(self) -> dict:
        """``{pane_id: (x, y, width, height)}`` across every live tab."""
        out: dict = {}
        for tab in self.tabs.values():
            for entry in tab.layout_payload()["result"]["layout"]["panes"]:
                rect = entry["rect"]
                out[entry["pane_id"]] = (
                    rect["x"], rect["y"], rect["width"], rect["height"]
                )
        return out

    # -- runner -----------------------------------------------------------
    def __call__(self, argv, capture_output=None, text=None, timeout=None, env=None, **_):
        tail = list(argv[1:])
        self.calls.append(tail)
        if tail[:2] == ["agent", "list"]:
            return self._done(argv, {"agents": self._rows()})
        if tail[:2] == ["pane", "layout"]:
            return self._pane_layout(argv, tail)
        if tail[:2] == ["pane", "move"]:
            return self._pane_move(argv, tail)
        if tail[:2] == ["pane", "resize"]:
            return self._pane_resize(argv, tail)
        raise AssertionError(f"unmodelled herdr command: {tail!r}")

    def _rows(self) -> list:
        rows = []
        for pane_id, name in self.agents.items():
            tab = self.tab_of(pane_id)
            decoded = decode_assigned_name(name)
            identity = decoded.identity if decoded.ok else None
            row = {
                "name": name,
                "pane_id": pane_id,
                # Production rows state the workspace explicitly as well as inside
                # the locator (measured on the operator's running herdr), and the
                # authority's scope test reads both (#14996 R2 review j#99938).
                "workspace_id": self.workspace_id,
                "agent_status": "idle",
                "tab_id": tab.tab_id if tab else "",
                # The detected provider is herdr's POSITIVE liveness signal; a stale
                # pane reports the field present-but-blank (shell residue).
                "agent": "" if pane_id in self.stale_panes else (
                    self.detected_override.get(
                        pane_id, identity.role if identity else ""
                    )
                ),
            }
            if identity:
                cwd = self.cwd_by_workspace.get(identity.workspace_id, "")
                if cwd:
                    row["foreground_cwd"] = cwd
            rows.append(row)
        return rows + list(self.extra_rows)

    def _pane_layout(self, argv, tail):
        pane_id = tail[tail.index("--pane") + 1] if "--pane" in tail else ""
        tab = self.tab_of(pane_id)
        if tab is None:
            return self._failed(argv, f"pane not found: {pane_id}")
        return self._done(argv, tab.layout_payload())

    def _pane_move(self, argv, tail):
        pane_id = tail[2]
        self._move_attempts += 1
        if (
            self.refuse_from_move is not None
            and self._move_attempts >= self.refuse_from_move
        ):
            return self._failed(argv, f"pane move refused: {pane_id}")
        if pane_id in self.move_refusals:
            return self._failed(argv, f"pane move refused: {pane_id}")
        source = self.tab_of(pane_id)
        if source is None:
            return self._failed(argv, f"pane not found: {pane_id}")
        if pane_id in self.move_unchanged:
            return self._done(
                argv, {"result": {"move_result": {"changed": False, "reason": "same_tab"}}}
            )
        if "--new-tab" in tail:
            source.remove(pane_id)
            target_tab = self.new_tab()
            target_tab.root = Leaf(pane_id)
            target_tab.focused = pane_id
        else:
            tab_id = tail[tail.index("--tab") + 1]
            direction = tail[tail.index("--split") + 1]
            target_tab = self.tabs.get(tab_id)
            if target_tab is None:
                return self._failed(argv, f"tab not found: {tab_id}")
            anchor = (
                tail[tail.index("--target-pane") + 1]
                if "--target-pane" in tail
                else target_tab.focused
            )
            if anchor not in target_tab.panes():
                return self._failed(argv, f"target pane not found: {anchor}")
            source.remove(pane_id)
            target_tab.subdivide(anchor, direction, pane_id)
        if source is not target_tab and not source.panes():
            # herdr auto-closes a tab whose last pane leaves.
            self.tabs.pop(source.tab_id, None)
        self._moves += 1
        rename = self.rename_after_moves.get(self._moves)
        if rename:
            self.agents[rename[0]] = rename[1]
        return self._done(
            argv,
            {
                "result": {
                    "move_result": {
                        "changed": True,
                        "pane": {"pane_id": pane_id, "tab_id": target_tab.tab_id},
                    }
                }
            },
        )

    def _pane_resize(self, argv, tail):
        self.resizes.append(tail)
        if self.resize_refused:
            return self._failed(argv, "pane resize refused")
        pane_id = tail[tail.index("--pane") + 1]
        direction = tail[tail.index("--direction") + 1]
        amount = float(tail[tail.index("--amount") + 1])
        tab = self.tab_of(pane_id)
        if tab is None:
            return self._failed(argv, f"pane not found: {pane_id}")
        if not self.resize_unchanged and not tab.resize(pane_id, direction, amount):
            return self._failed(argv, f"matching divider not found: {pane_id}")
        return self._done(argv, {"result": {"type": "ok"}})

    @staticmethod
    def _done(argv, payload):
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    @staticmethod
    def _failed(argv, message):
        return subprocess.CompletedProcess(argv, 1, "", message)


__all__ = ("Leaf", "PaneTreeHerdr", "Rect", "Split", "Tab")
