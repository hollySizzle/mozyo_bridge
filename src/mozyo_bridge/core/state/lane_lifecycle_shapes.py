"""Lane lifecycle — the exact per-version column shapes and column definitions (#14477 j#94582).

Extracted from :mod:`mozyo_bridge.core.state.lane_lifecycle_schema` as a leaf so each module
stays a cohesive, under-threshold unit — the same reason
:mod:`mozyo_bridge.core.state.lane_lifecycle_rows` exists ("the design's 'one component'
boundary is about the data model, not the Python module"). This is a **pure move**: the tables,
their comments and their values are byte-identical to what the schema module carried, and this
module holds no logic.

It is the single authority the read-compatibility and migration paths consult: a recognized
recorded version must match one of ITS version's allowed signatures EXACTLY (set equality — no
unknown extra columns, no missing columns), and every column must carry the exact
authority-affecting definition ``PRAGMA table_info`` reports. Nothing here guesses a
compatibility; a shape the tables do not describe fails closed at the caller.
"""

from __future__ import annotations

from typing import Optional


_V1_COLUMNS = frozenset(
    {
        "repo_workspace_id",
        "lane_id",
        "issue_id",
        "lane_disposition",
        "process_release",
        "revision",
        "release_action_id",
        "release_pins",
        "decision_source",
        "decision_journal",
        "created_at",
        "updated_at",
    }
)
_V2_ADDS = frozenset({"decision_issue_id"})  # R2-F1: split the decision anchor's issue
_V3_ADDS = frozenset(
    {"replacement_state", "replacement_action_id", "replacement_pins"}  # #13763
)
_V4_ADDS = frozenset({"worktree_identity"})  # #13754 worktree binding
_V5_ADDS = frozenset(
    {"binding_kind", "project_scope", "lane_generation", "declared_slots"}  # #13810
)
_V6_ADDS = frozenset({"reconcile_phase"})  # #13842 reconcile owed-close provenance
_V7_ADDS = frozenset({"lane_kind"})  # #13647 generation-bound lane-role heal authority
_V8_ADDS = frozenset({"hibernated_at"})  # #14477 immutable resume-freshness boundary
_V9_ADDS = frozenset({"release_observation"})  # #14477 j#94582 release-observation authority
_V10_ADDS = frozenset({"lane_epoch"})  # #14756 monotonic hibernate-generation epoch
_V11_ADDS = frozenset({"reconcile_close_pin"})  # #15227 generation-bound owed close

#: The EXACT allowed column-name signatures per recorded version (Redmine #13754 R6-F1,
#: j#78803). A recognized store must match one of its version's signatures EXACTLY (set
#: equality — no unknown extra columns, no missing columns), or it is a partial /
#: incompatible authority shape and fails closed (never silently re-created, migrated, or
#: re-stamped). v3 is the collision point where TWO branches legitimately exist — the
#: staging replacement-v3 and the already-live #13754 worktree-v3 — so v3 allows exactly
#: those two shapes and NOTHING ELSE (a pure-v2 shape recorded v3 is NOT a known v3).
_SHAPE_V1 = _V1_COLUMNS
_SHAPE_V2 = _V1_COLUMNS | _V2_ADDS
_SHAPE_V3_REPLACEMENT = _V1_COLUMNS | _V2_ADDS | _V3_ADDS
_SHAPE_V3_WORKTREE = _V1_COLUMNS | _V2_ADDS | _V4_ADDS
_SHAPE_V4 = _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS
_SHAPE_V5 = _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS | _V5_ADDS
_SHAPE_V6 = _V1_COLUMNS | _V2_ADDS | _V3_ADDS | _V4_ADDS | _V5_ADDS | _V6_ADDS
_SHAPE_V7 = _SHAPE_V6 | _V7_ADDS
_SHAPE_V8 = _SHAPE_V7 | _V8_ADDS
_SHAPE_V9 = _SHAPE_V8 | _V9_ADDS
_SHAPE_V10 = _SHAPE_V9 | _V10_ADDS
_SHAPE_V11 = _SHAPE_V10 | _V11_ADDS
_ALLOWED_SHAPES_BY_VERSION: dict[int, tuple[frozenset, ...]] = {
    1: (_SHAPE_V1,),
    2: (_SHAPE_V2,),
    3: (_SHAPE_V3_REPLACEMENT, _SHAPE_V3_WORKTREE),
    4: (_SHAPE_V4,),
    5: (_SHAPE_V5,),
    6: (_SHAPE_V6,),
    7: (_SHAPE_V7,),
    8: (_SHAPE_V8,),
    9: (_SHAPE_V9,),
    10: (_SHAPE_V10,),
    11: (_SHAPE_V11,),
}

#: The authority-affecting definition each column MUST carry: ``(type, notnull, default,
#: pk_order)`` as ``PRAGMA table_info`` reports it. A same-named but re-typed / nullable /
#: default-changed / PK-shifted column is NOT the current column — it fails closed rather
#: than being read as authoritative (R6-F1). ``pk_order`` is the 1-based composite-PK
#: position (0 for a non-PK column); ``default`` is the SQL literal ``table_info`` returns.
_COLUMN_DEFS: dict[str, tuple[str, int, Optional[str], int]] = {
    "repo_workspace_id": ("TEXT", 1, None, 1),
    "lane_id": ("TEXT", 1, None, 2),
    "issue_id": ("TEXT", 1, "''", 0),
    "lane_disposition": ("TEXT", 1, None, 0),
    "process_release": ("TEXT", 1, None, 0),
    "revision": ("INTEGER", 1, None, 0),
    "release_action_id": ("TEXT", 1, "''", 0),
    "release_pins": ("TEXT", 1, "''", 0),
    "replacement_state": ("TEXT", 1, "'not_requested'", 0),
    "replacement_action_id": ("TEXT", 1, "''", 0),
    "replacement_pins": ("TEXT", 1, "''", 0),
    "decision_source": ("TEXT", 1, "''", 0),
    "decision_issue_id": ("TEXT", 1, "''", 0),
    "decision_journal": ("TEXT", 1, "''", 0),
    "created_at": ("TEXT", 1, None, 0),
    "updated_at": ("TEXT", 1, None, 0),
    "worktree_identity": ("TEXT", 1, "''", 0),
    "binding_kind": ("TEXT", 1, "'issue'", 0),
    "project_scope": ("TEXT", 1, "''", 0),
    "lane_generation": ("INTEGER", 1, "1", 0),
    "declared_slots": ("TEXT", 1, "''", 0),
    "reconcile_phase": ("TEXT", 1, "''", 0),
    "reconcile_close_pin": ("TEXT", 1, "''", 0),
    "lane_kind": ("TEXT", 1, "''", 0),
    "hibernated_at": ("TEXT", 1, "''", 0),
    "release_observation": ("TEXT", 1, "''", 0),
    # #14756: the monotonic hibernate-generation epoch, stored as CANONICAL DECIMAL TEXT in
    # a BLOB (i.e. NONE affinity) column. The declared type is load-bearing and was measured:
    # under the INTEGER affinity this first used, SQLite coerced ``'00'``, ``'+0'``, ``' 0 '``,
    # ``'0.0'``, ``2.0``, ``False`` and ``True`` into integers on the way in, so each read back
    # as a legitimate counter and re-minted an epoch — a rollback that re-issues a value a
    # released generation may still hold (j#96911 F2). No Python predicate can reject a
    # conversion that already happened, so the storage class is the guarantee: the bytes
    # written are the bytes returned, and the CAS matches on both the bytes and
    # ``typeof(lane_epoch) = 'text'``. ``'0'`` is the migration default and means NO EPOCH HAS
    # EVER BEEN MINTED; it is never a substitute boundary (see :mod:`...lane_epoch`).
    "lane_epoch": ("BLOB", 1, "'0'", 0),
}
