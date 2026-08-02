"""The #13686 actuator's durable step ledger (Redmine #14825, acceptance item 4).

#13686 shipped :class:`~...application.auto_integration_ports.InMemoryLedgerStore` and said
plainly what it was not: "its lifetime is one actuator instance, so a resume across processes
finds nothing and the run starts over". For the integration machine that is not merely a missing
optimisation — the CI gate is asynchronous, so the run that pushes and the run that concludes
``integrated`` are DIFFERENT PROCESSES by construction. Without a ledger that outlives a process
there is no continuation, and the feature cannot complete.

This is that store. Three properties, each of which #13686 named as owed to this issue.

**An outcome may only be recorded by the run that was admitted to produce it.** R1 stamped the
store's ``writer_id`` on whatever ``StepOutcome`` a caller passed, and review j#96611 finding 3
reproduced the consequence: ``append`` of a bare ``done`` push with ``push_status=accepted``, no
mutation anywhere, produced a row indistinguishable from a real receipt — and
``ledger_authorizing_action_reader`` accepted it as a cleanup's authorizing action. Stamping a
provenance onto an unexamined payload authenticates the FILE, not the CLAIM.

So the receipt is now bound to an admission. :meth:`begin_step` mints a one-time token into an
open intent row and returns it; :meth:`append` requires a token matching an OPEN intent for
exactly that ``(action_key, step)``, and closes it in the same transaction. There is no way to
record an outcome for a step nobody was admitted to perform.

*What that boundary is, stated exactly.* The OS account that owns the repository, Git/Redmine
credentials and this file is the trusted principal (owner decision j#96706). Two mutually
distrusting processes under that same account cannot authenticate each other through a shared
file; an underscore, an in-process object, mode 0600, a same-user socket or a secret stored beside
the DB does not change that. The private writer surface is therefore a misuse boundary, not a
security boundary. What the admission enforces inside that trust boundary is exclusive ordering:
a second run is refused before it mutates, replay is rejected, and a crash is explicit and
reconcilable. Protecting against malicious same-UID code would require moving the ledger AND the
Git/Redmine mutation credentials to a distinct OS security principal; it is not claimed here.

**One run at a time may be admitted to a step.** R1 read :meth:`unresolved_intents` and then
called ``begin_step`` — a check-then-write with no constraint behind it, so two runs both opened
an intent for the same push and both proceeded to mutate (reproduced, j#96611 finding 4; the
``done`` unique index only catches the second one AFTER its mutation). A partial unique index on
the OPEN intents makes the admission itself the compare-and-set: the second ``begin_step`` is
refused, before any side effect.

**A crash between a mutation and its receipt is recoverable, not merely detectable.** The
crashed run's token died with it, so recovery cannot present one — :meth:`resolve_intent` closes
an open intent without a token and records what the RECONCILER MEASURED, marking the row
``reconciled`` so the durable record never confuses "the run that did it said so" with
"a later run went and looked". It still requires an open intent to exist, so it is not a second
way to invent a receipt. Review j#96611 finding 5: R1 detected the state and then blocked
forever, which is not the replay-safe recovery the acceptance asks for.

**A ``done`` step is recorded once per action.** A partial unique index makes a second ``done``
row for the same ``(action_key, step)`` a database error rather than a second entry the ledger
would then report as a duplicate step. The idempotency contract is enforced by the store, not
only checked by the reader afterwards.

The store is append-only: there is no update and no delete on the outcome table, and closing an
intent writes to the intent table alone.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Tuple

from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_action_registry import (  # noqa: E501
    ACTION_AWAITING_CI,
    ACTION_CI_FAILED,
    ACTION_INDEX_SQL,
    ACTION_INTEGRATED,
    ACTION_REGISTERED,
    ACTION_SCHEMA_SQL,
    ACTION_STATES,
    ActionRegistryError,
    DurableIntegrationAction,
    action_event_count as _action_event_count,
    mark_action_awaiting_ci as _mark_action_awaiting_ci,
    mark_action_terminal as _mark_action_terminal,
    read_action as _read_action,
    register_action as _register_action,
    resumable_actions as _resumable_actions,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_PENDING,
    StepOutcome,
)

#: What a reconciliation may record. A measurement settles a question, so ``pending`` is not an
#: answer it can give — and an outcome outside this set is a caller inventing a vocabulary.
_RECONCILABLE_OUTCOMES = frozenset({OUTCOME_DONE, OUTCOME_BLOCKED})

#: The ledger file, alongside the other home-scoped durable stores.
AUTO_INTEGRATION_LEDGER_FILENAME = "auto_integration_ledger.sqlite3"

#: Bumped only for an incompatible layout. A file written by a newer schema is left untouched
#: rather than opened optimistically (the same downgrade-safe posture as the herdr ledger).
#:
#: v2 (Redmine #14825 review j#96611): the intent row carries a one-time ``receipt`` an append
#: must present, and the open intents are uniquely constrained. There is no migration because
#: there is nothing to migrate — this feature has never run, so no v1 file exists outside a test
#: that creates one; a v1 file is refused rather than silently upgraded, which is the same
#: downgrade-safe posture in the other direction.
AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION = 3

_WRITER_SQL = """
CREATE TABLE IF NOT EXISTS auto_integration_writer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    writer_id TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_OUTCOME_SQL = """
CREATE TABLE IF NOT EXISTS auto_integration_ledger (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    action_key TEXT NOT NULL,
    step TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    head TEXT NOT NULL DEFAULT '',
    git_version TEXT NOT NULL DEFAULT '',
    merge_status TEXT NOT NULL DEFAULT '',
    push_status TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL
)
"""

_INTENT_SQL = """
CREATE TABLE IF NOT EXISTS auto_integration_intent (
    id INTEGER PRIMARY KEY,
    opened_at TEXT NOT NULL,
    action_key TEXT NOT NULL,
    step TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    receipt TEXT NOT NULL,
    resolved_at TEXT,
    closed_by TEXT,
    observation TEXT
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_auto_integration_ledger_action "
    "ON auto_integration_ledger(action_key, id)",
    # The idempotency contract, enforced where the write happens: one `done` per step per action.
    # A second one is refused by the database, so "the ledger records a duplicate step" cannot be
    # produced by this store at all — a reader that finds one is looking at a foreign file.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_integration_ledger_done "
    "ON auto_integration_ledger(action_key, step) WHERE outcome = 'done'",
    # The ADMISSION, as a compare-and-set rather than a check followed by a write (j#96611
    # finding 4). At most one OPEN intent may exist for a step of an action, so the second
    # concurrent `begin_step` is refused by the database BEFORE its caller mutates anything.
    # Resolved rows are excluded, so the history of an action's steps is unbounded while its
    # admission is exclusive.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_integration_intent_admission "
    "ON auto_integration_intent(action_key, step) WHERE resolved_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_auto_integration_intent_open "
    "ON auto_integration_intent(action_key, resolved_at)",
) + ACTION_INDEX_SQL

#: How an intent was closed. ``receipt`` is the run that was admitted presenting its own token;
#: ``reconciled`` is a LATER run that measured the world because the admitted run never came
#: back. Kept apart in the durable record because they are different evidence: one is the actor
#: saying what it did, the other is an observer saying what it found.
CLOSED_BY_RECEIPT = "receipt"
CLOSED_BY_RECONCILIATION = "reconciled"

_COLUMNS = (
    "recorded_at, action_key, step, outcome, detail, head, git_version, "
    "merge_status, push_status, recorded_by"
)


class AutoIntegrationLedgerError(RuntimeError):
    """The ledger could not be opened or written. Fail-closed: never "nothing has run"."""


class AutoIntegrationAdmissionError(AutoIntegrationLedgerError):
    """Another run holds the admission for this step. NOTHING was mutated.

    A distinct type because the caller's response is distinct: this is not an error to retry
    past, it is the compare-and-set doing its job. Whoever holds the admission is running, or
    crashed while running, and either way a second mutation is exactly what must not happen.
    """


class _Unadmitted(Exception):
    """Internal: the write has no valid admission behind it (never escapes this module)."""


@dataclass(frozen=True)
class StepIntent:
    """A step this store was told was about to run, and whose outcome it never received.

    Its existence is the honest statement of an unknown: the side effect may have happened. A
    consumer must not read it as "the step did not run" — that reading is what turns a crash
    between a push and its receipt into a second push.
    """

    action_key: str
    step: str
    opened_at: str
    intent_id: int = 0
    recorded_by: str = ""
    #: The one-time token :meth:`SqliteLedgerStore.append` requires to close this admission.
    #: Held only by the run that was admitted; a crash takes it with it, which is why recovery
    #: goes through :meth:`SqliteLedgerStore.resolve_intent` instead of presenting one.
    receipt: str = ""


# The production mutation capability is deliberately module-private.  A public
# ``SqliteLedgerStore`` is a reader: constructing one no longer gives arbitrary code the
# begin+append pair that review j#96650 used to forge an accepted push.  The composition root
# mints the writer and keeps it inside the use case/reconciler boundary.
_LEDGER_WRITER_CAPABILITY = object()


def auto_integration_ledger_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / AUTO_INTEGRATION_LEDGER_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AutoIntegrationLedgerReader:
    """The ledger's READ surface, with no way to write through it (j#96650 finding 1).

    The authorization derivation holds one of these. R2 handed it the whole store, so the
    module that decides "which action authorized this cleanup" was also able to manufacture the
    row it then read. Separating the capability does not make a shared file authenticate its
    writers — nothing in one process can — but it removes the shape where a read path carries a
    write capability it never needs.

    **This is one of two halves.** The other is that the authorization is corroborated against a
    record the ledger's writer does not control (the coordinator's integration disposition, read
    from the tracker), so a forged ledger row alone establishes nothing. Neither half is
    sufficient; the finding is answered by both.
    """

    store: "SqliteLedgerStore"

    def read(self, *, action_key: str) -> Sequence[StepOutcome]:
        return self.store.read(action_key=action_key)

    def completed_action_keys(self, *, prefix: str, step: str) -> Tuple[str, ...]:
        return self.store.completed_action_keys(prefix=prefix, step=step)


class SqliteLedgerStore:
    """A durable, append-only :class:`...auto_integration_ports.LedgerStore`.

    Satisfies the port's contract — whole :class:`StepOutcome` records including their
    provenance, and entries only ever from :meth:`append` — and adds the two things a
    cross-process, asynchronously-gated action needs: a writer identity the store owns, and an
    intent record that survives a crash between a mutation and its receipt.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        home: Optional[Path] = None,
        _writer_capability: object = None,
    ) -> None:
        self.path = path or auto_integration_ledger_path(home)
        self._writer_id = ""
        self._writer_enabled = _writer_capability is _LEDGER_WRITER_CAPABILITY

    def _require_writer(self) -> None:
        if not self._writer_enabled:
            raise AutoIntegrationLedgerError(
                "this SqliteLedgerStore is a read capability; mutation is owned by the "
                "production actuator/reconciler and is not available from the public store"
            )

    # -- identity ---------------------------------------------------------

    @property
    def writer_id(self) -> str:
        """This store's own writer identity, minted into its file at creation.

        Read through the connection rather than generated per instance, so two processes opening
        the same ledger agree about which entries are the actuator's — which is the whole
        mechanism a resume across the asynchronous CI gate rests on.
        """
        if not self._writer_id:
            if not self.path.exists() and not self._writer_enabled:
                raise AutoIntegrationLedgerError(
                    "the auto-integration ledger does not exist; a read capability never "
                    "creates its authority store"
                )
            conn = self._connect(read_only=not self._writer_enabled)
            try:
                self._writer_id = self._read_writer(conn)
            finally:
                conn.close()
        return self._writer_id

    # -- port ---------------------------------------------------------------

    def read(self, *, action_key: str) -> Sequence[StepOutcome]:
        """Every entry recorded under exactly ``action_key``, oldest first.

        A missing file is an empty ledger (nothing has been recorded yet), which is the correct
        fail-closed reading: the run starts over rather than trusting anything. An UNREADABLE
        file is a different fact and raises — "we cannot read what ran" must not present as
        "nothing ran", because the next thing the caller would do is push again.
        """
        if not self.path.exists():
            return ()
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM auto_integration_ledger "
                "WHERE action_key = ? ORDER BY id",
                (str(action_key),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return tuple(_row_to_outcome(row) for row in rows)

    def append(self, outcome: StepOutcome, *, receipt: str = "") -> None:
        """Record ``outcome`` against the ADMISSION that produced it.

        ``receipt`` is the token :meth:`begin_step` minted for exactly this
        ``(action_key, step)``. Without a matching OPEN intent the write is refused: an outcome
        for a step nobody was admitted to perform is not a receipt, whatever it says about
        itself (j#96611 finding 3 reproduced a forged ``done`` push landing here).

        ``outcome.recorded_by`` is still not read. A payload's own claim about who wrote it is
        the self-report this store exists to replace, and accepting it "when it happens to
        match" would make the check depend on the value being checked. The store stamps its own
        writer id, and the admission is what makes that stamp mean something.

        The row and the closing of its intent are ONE transaction, so the receipt and the intent
        cannot come apart the way the mutation and the receipt can.
        """
        self._require_writer()
        token = str(receipt or "")
        conn = self._connect()
        try:
            writer = self._read_writer(conn)
            with conn:
                intent = conn.execute(
                    "SELECT id, receipt FROM auto_integration_intent WHERE action_key = ? "
                    "AND step = ? AND resolved_at IS NULL",
                    (str(outcome.action_key), str(outcome.step)),
                ).fetchone()
                if intent is None:
                    raise _Unadmitted(
                        f"no open admission for step {outcome.step!r} of this action; an "
                        "outcome may only be recorded by the run that was admitted to produce "
                        "it (call begin_step first, or resolve_intent to reconcile a crash)"
                    )
                if not token or not secrets.compare_digest(str(intent[1]), token):
                    raise _Unadmitted(
                        f"the receipt offered for step {outcome.step!r} is not the one this "
                        "store minted for the open admission; refusing to record an outcome "
                        "on somebody else's admission"
                    )
                if outcome.outcome == OUTCOME_PENDING and conn.execute(
                    "SELECT 1 FROM auto_integration_ledger WHERE action_key = ? AND step = ? "
                    "AND outcome = ? LIMIT 1",
                    (str(outcome.action_key), str(outcome.step), OUTCOME_PENDING),
                ).fetchone():
                    # Already recorded as pending: close the admission and write NOTHING.
                    # Review j#96650 finding 3 measured the alternative — each re-entry of an
                    # unsettled asynchronous gate appended another row (2 -> 3), while the
                    # continuation's own detail said it "recorded no progress". An observation
                    # that has not changed is not a new observation, and a durable record that
                    # grows on every poll is a durable record of the polling.
                    #
                    # Scoped to `pending` on purpose. A repeated `blocked` IS new information —
                    # the step was attempted again and refused again — so those still accumulate.
                    conn.execute(
                        "UPDATE auto_integration_intent SET resolved_at = ?, closed_by = ? "
                        "WHERE id = ?",
                        (_utc_now(), CLOSED_BY_RECEIPT, int(intent[0])),
                    )
                    return
                conn.execute(
                    f"INSERT INTO auto_integration_ledger ({_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 10)})",
                    (
                        _utc_now(),
                        str(outcome.action_key),
                        str(outcome.step),
                        str(outcome.outcome),
                        str(outcome.detail),
                        str(outcome.head),
                        str(outcome.git_version),
                        str(outcome.merge_status),
                        str(outcome.push_status),
                        writer,
                    ),
                )
                conn.execute(
                    "UPDATE auto_integration_intent SET resolved_at = ?, closed_by = ? "
                    "WHERE id = ?",
                    (_utc_now(), CLOSED_BY_RECEIPT, int(intent[0])),
                )
        except _Unadmitted as exc:
            raise AutoIntegrationLedgerError(str(exc)) from None
        except sqlite3.IntegrityError as exc:
            raise AutoIntegrationLedgerError(
                f"step {outcome.step!r} is already recorded {OUTCOME_DONE} for this action; "
                "refusing to record it twice (the idempotency key is the action key)"
            ) from exc
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be written "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()

    def completed_action_keys(self, *, prefix: str, step: str) -> Tuple[str, ...]:
        """Action keys under ``prefix`` whose ``step`` this store recorded ``done``, in order.

        The lifecycle authority a post-close cleanup is authorized BY. #13686's cleanup compared
        the authorization it was offered against the record that offered it — a comparison whose
        two sides were the same field, so it could not fail (Redmine #14825 item 5). The
        independent side is here: the ledger says which integration action actually ran to
        completion, and it says so in rows this store stamped, not in the record asking to act.

        ``prefix`` matches the action key's leading identity fields exactly (they are ordered, so
        a prefix IS an identity constraint); ``_`` and ``%`` in it are escaped so a caller's
        value cannot widen the match into a pattern.
        """
        if not self.path.exists():
            return ()
        pattern = _like_prefix(prefix)
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT action_key FROM auto_integration_ledger "
                "WHERE step = ? AND outcome = ? AND action_key LIKE ? ESCAPE '\\' "
                "ORDER BY action_key",
                (str(step), OUTCOME_DONE, pattern),
            ).fetchall()
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return tuple(str(row[0]) for row in rows)

    # -- crash boundary ----------------------------------------------------

    def begin_step(self, *, action_key: str, step: str) -> StepIntent:
        """Claim the exclusive admission to run ``step``, before the side effect happens.

        Two things at once, and both are load-bearing:

        - it records that a mutation is ABOUT to happen, so a crash before the receipt leaves
          "we do not know" in the durable record rather than an absence the next run reads as
          "not yet done" and offers to do again;
        - it is the COMPARE-AND-SET that admits exactly one run to that step. R1 left this as a
          plain insert behind a separate ``unresolved_intents`` read, and two runs were both
          admitted to the same push (reproduced, j#96611 finding 4). The unique index over the
          OPEN intents is what makes the second one fail here, before it mutates.

        Raises :class:`AutoIntegrationAdmissionError` when another run already holds the
        admission. A caller must treat that as "do not mutate" — it is not a retryable error,
        it is somebody else's turn.
        """
        self._require_writer()
        conn = self._connect()
        try:
            writer = self._read_writer(conn)
            opened_at = _utc_now()
            token = f"receipt:{secrets.token_hex(16)}"
            with conn:
                cursor = conn.execute(
                    "INSERT INTO auto_integration_intent "
                    "(opened_at, action_key, step, recorded_by, receipt) VALUES (?, ?, ?, ?, ?)",
                    (opened_at, str(action_key), str(step), writer, token),
                )
                intent_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise AutoIntegrationAdmissionError(
                f"another run already holds the admission for step {step!r} of this action; "
                "refusing to admit a second one (nothing was mutated)"
            ) from exc
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not record a step intent "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return StepIntent(
            action_key=str(action_key),
            step=str(step),
            opened_at=opened_at,
            intent_id=intent_id,
            recorded_by=writer,
            receipt=token,
        )

    def resolve_intent(
        self,
        *,
        intent_id: int,
        action_key: str,
        step: str,
        resolution: StepOutcome,
        observation: str,
    ) -> None:
        """Close a STRANDED admission with what a later run MEASURED (the recovery path).

        The crashed run's token died with it, so recovery cannot present one — requiring it here
        would make the state permanent, which is what R1 shipped and what j#96611 finding 5
        rejected. Two things keep this from becoming a second way to forge a receipt:

        - an OPEN intent must exist. This closes a step somebody was admitted to run; it cannot
          invent an outcome for a step nobody ever began;
        - the row is marked :data:`CLOSED_BY_RECONCILIATION` and carries the ``observation`` the
          reconciler made, so the durable record never confuses "the run that did it said so"
          with "a later run went and looked". A reader that cares about the difference can see
          it.

        ``resolution`` is the outcome the measurement supports — ``done`` when the mutation is
        observed to have landed, ``blocked`` when it is observed not to have. A reconciler that
        cannot tell must not call this at all: ambiguity stays open, which is the honest state.

        Two identity constraints, both added after review j#96650 reproduced what their absence
        cost:

        - **``resolution`` must be about the admission being closed** (finding 1). R2 searched
          for the intent by ``(action_key, step)`` and then inserted whatever ``resolution``
          named, so ONE open admission on a decoy action could write an ``accepted`` push for a
          DIFFERENT action — a cross-action forgery introduced by the very path added to close
          a forgery. The outcome vocabulary is closed here too: a reconciliation records a
          settled fact, never ``pending``.
        - **the row closed is the row that was OBSERVED** (finding 2). R2 re-searched for
          "whatever is open now", so a reconciliation begun against one run could close a
          LATER run's admission — reproduced, leaving that run's mutation unrecorded and its
          receipt refused. ``intent_id`` is the observed instance, and the update is a
          compare-and-set on it still being open.
        """
        self._require_writer()
        if str(resolution.action_key) != str(action_key) or str(resolution.step) != str(step):
            raise AutoIntegrationLedgerError(
                "a reconciliation may only record an outcome for the admission it closes; "
                f"asked to close {action_key!r}/{step!r} while recording "
                f"{resolution.action_key!r}/{resolution.step!r}"
            )
        if resolution.outcome not in _RECONCILABLE_OUTCOMES:
            raise AutoIntegrationLedgerError(
                f"a reconciliation records a settled outcome ({sorted(_RECONCILABLE_OUTCOMES)}), "
                f"not {resolution.outcome!r}"
            )
        conn = self._connect()
        try:
            writer = self._read_writer(conn)
            with conn:
                intent = conn.execute(
                    "SELECT id FROM auto_integration_intent WHERE id = ? AND action_key = ? "
                    "AND step = ? AND resolved_at IS NULL",
                    (int(intent_id), str(action_key), str(step)),
                ).fetchone()
                if intent is None:
                    raise _Unadmitted(
                        f"admission {intent_id} for step {step!r} of this action is not open; "
                        "reconciliation closes the exact admission it observed, so a later "
                        "run's admission is not it either"
                    )
                conn.execute(
                    f"INSERT INTO auto_integration_ledger ({_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 10)})",
                    (
                        _utc_now(),
                        str(resolution.action_key),
                        str(resolution.step),
                        str(resolution.outcome),
                        str(resolution.detail),
                        str(resolution.head),
                        str(resolution.git_version),
                        str(resolution.merge_status),
                        str(resolution.push_status),
                        writer,
                    ),
                )
                conn.execute(
                    "UPDATE auto_integration_intent SET resolved_at = ?, closed_by = ?, "
                    "observation = ? WHERE id = ?",
                    (
                        _utc_now(),
                        CLOSED_BY_RECONCILIATION,
                        str(observation),
                        int(intent[0]),
                    ),
                )
        except _Unadmitted as exc:
            raise AutoIntegrationLedgerError(str(exc)) from None
        except sqlite3.IntegrityError as exc:
            raise AutoIntegrationLedgerError(
                f"step {resolution.step!r} is already recorded {OUTCOME_DONE} for this action; "
                "the reconciliation would duplicate it"
            ) from exc
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be reconciled "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()

    def unresolved_intents(self, *, action_key: str) -> Tuple[StepIntent, ...]:
        """Steps whose side effect may have run and whose outcome never arrived."""
        if not self.path.exists():
            return ()
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                "SELECT id, opened_at, action_key, step, recorded_by FROM "
                "auto_integration_intent WHERE action_key = ? AND resolved_at IS NULL "
                "ORDER BY id",
                (str(action_key),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} could not be read "
                f"({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return tuple(
            StepIntent(
                intent_id=int(row[0]),
                opened_at=str(row[1]),
                action_key=str(row[2]),
                step=str(row[3]),
                recorded_by=str(row[4]),
            )
            for row in rows
        )

    # -- durable async-action registry -----------------------------------

    def register_action(self, action: DurableIntegrationAction) -> None:
        """Persist an immutable action frame before any integration mutation."""
        try:
            _register_action(self, action)
        except ActionRegistryError as exc:
            raise AutoIntegrationLedgerError(str(exc)) from exc

    def mark_action_awaiting_ci(
        self, *, action_key: str, landed_head: str, ci_workflow: str
    ) -> None:
        """Append the one ``awaiting_ci`` transition, or no-op on exact replay."""
        try:
            _mark_action_awaiting_ci(
                self,
                action_key=action_key,
                landed_head=landed_head,
                ci_workflow=ci_workflow,
            )
        except ActionRegistryError as exc:
            raise AutoIntegrationLedgerError(str(exc)) from exc

    def mark_action_terminal(
        self, *, action_key: str, state: str, landed_head: str, detail: str = ""
    ) -> None:
        """Append one terminal continuation result under an awaiting action."""
        try:
            _mark_action_terminal(
                self,
                action_key=action_key,
                state=state,
                landed_head=landed_head,
                detail=detail,
            )
        except ActionRegistryError as exc:
            raise AutoIntegrationLedgerError(str(exc)) from exc

    def action(self, action_key: str) -> Optional[DurableIntegrationAction]:
        """The immutable frame plus latest append-only transition for one action."""
        try:
            return _read_action(self, action_key)
        except ActionRegistryError as exc:
            raise AutoIntegrationLedgerError(str(exc)) from exc

    def resumable_actions(
        self, *, workspace: str = "", issue: str = ""
    ) -> Tuple[DurableIntegrationAction, ...]:
        """Registered/awaiting actions a supervisor may inspect, oldest first."""
        try:
            return _resumable_actions(self, workspace=workspace, issue=issue)
        except ActionRegistryError as exc:
            raise AutoIntegrationLedgerError(str(exc)) from exc

    def action_event_count(self, *, action_key: str) -> int:
        """Read-only idempotency diagnostic used by regression tests and operators."""
        try:
            return _action_event_count(self, action_key=action_key)
        except ActionRegistryError as exc:
            raise AutoIntegrationLedgerError(str(exc)) from exc

    # -- connection --------------------------------------------------------

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._check_version(conn)
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA busy_timeout = 2000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with conn:
                conn.execute(_WRITER_SQL)
                conn.execute(_OUTCOME_SQL)
                conn.execute(_INTENT_SQL)
                for sql in ACTION_SCHEMA_SQL:
                    conn.execute(sql)
                for sql in _INDEX_SQL:
                    conn.execute(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO auto_integration_writer "
                    "(id, writer_id, created_at) VALUES (1, ?, ?)",
                    (f"ledger:{secrets.token_hex(16)}", _utc_now()),
                )
                conn.execute(
                    f"PRAGMA user_version = {AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION}"
                )
            return conn
        if version == 2:
            # v2 was created by the unreleased R2 implementation and contains only writer /
            # outcome / intent rows.  v3 adds the immutable action registry and append-only
            # continuation events; the migration deletes or rewrites nothing.
            with conn:
                for sql in ACTION_SCHEMA_SQL:
                    conn.execute(sql)
                for sql in _INDEX_SQL:
                    conn.execute(sql)
                conn.execute(
                    f"PRAGMA user_version = {AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION}"
                )
            return conn
        self._check_version(conn, opened=version)
        return conn

    def _check_version(
        self, conn: sqlite3.Connection, *, opened: Optional[int] = None
    ) -> None:
        version = (
            opened
            if opened is not None
            else conn.execute("PRAGMA user_version").fetchone()[0]
        )
        if version != AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION:
            conn.close()
            raise AutoIntegrationLedgerError(
                f"auto-integration ledger {self.path} has schema version {version}; this "
                f"mozyo-bridge supports {AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION}. The file is "
                "left untouched (downgrade-safe)."
            )

    @staticmethod
    def _read_writer(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT writer_id FROM auto_integration_writer WHERE id = 1"
        ).fetchone()
        if not row or not str(row[0] or "").strip():
            raise AutoIntegrationLedgerError(
                "the auto-integration ledger carries no writer identity; refusing to attribute "
                "entries to an identity that was never minted"
            )
        return str(row[0])


def _like_prefix(prefix: str) -> str:
    """``prefix`` as a LIKE pattern matching it literally, then anything.

    SQL ``LIKE`` treats ``%`` and ``_`` as wildcards, and an action key is caller data (a
    ``target_ref`` may legitimately contain either). Escaping them keeps a prefix a prefix
    rather than letting one widen into a pattern that matches a DIFFERENT action.
    """
    escaped = (
        str(prefix).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"{escaped}%"


def _row_to_outcome(row: Sequence[object]) -> StepOutcome:
    return StepOutcome(
        action_key=str(row[1]),
        step=str(row[2]),
        outcome=str(row[3]),
        detail=str(row[4]),
        head=str(row[5]),
        git_version=str(row[6]),
        merge_status=str(row[7]),
        push_status=str(row[8]),
        recorded_by=str(row[9]),
    )


def _open_ledger_writer(
    path: Optional[Path] = None, *, home: Optional[Path] = None
) -> SqliteLedgerStore:
    """Mint the module-private production mutation capability inside the trusted OS account.

    Deliberately omitted from ``__all__`` so ordinary callers receive only the read surface. This
    reduces accidental misuse; Python module privacy is not process authentication, and the
    module-level threat-model statement above is the exact boundary.
    """
    writer = SqliteLedgerStore(
        path=path, home=home, _writer_capability=_LEDGER_WRITER_CAPABILITY
    )
    # Create/migrate before a separately constructed reader is handed to the use case.
    _ = writer.writer_id
    return writer


__all__ = (
    "ACTION_AWAITING_CI",
    "ACTION_CI_FAILED",
    "ACTION_INTEGRATED",
    "ACTION_REGISTERED",
    "ACTION_STATES",
    "AUTO_INTEGRATION_LEDGER_FILENAME",
    "AutoIntegrationLedgerReader",
    "AUTO_INTEGRATION_LEDGER_SCHEMA_VERSION",
    "CLOSED_BY_RECEIPT",
    "CLOSED_BY_RECONCILIATION",
    "AutoIntegrationAdmissionError",
    "AutoIntegrationLedgerError",
    "DurableIntegrationAction",
    "SqliteLedgerStore",
    "StepIntent",
    "auto_integration_ledger_path",
)
