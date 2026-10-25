"""Synchronous repositories built on short SQLite transactions."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from minimal_agent.schemas import (
    MessageKind,
    MessageRecord,
    MessageRole,
    PendingStreamEvent,
    RunRecord,
    RunStatus,
    SessionRecord,
    StreamEventRecord,
    TraceRecord,
)
from minimal_agent.storage.database import Database


class RepositoryError(RuntimeError):
    """Base class for expected persistence failures."""


class NotFoundError(RepositoryError):
    """Raised when a requested durable record does not exist."""


class ActiveRunExistsError(RepositoryError):
    """Raised when a session already owns a running run."""


class InvalidStateError(RepositoryError):
    """Raised when a requested transition would violate lifecycle rules."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _load_payload(payload_json: str) -> dict[str, Any]:
    value = json.loads(payload_json)
    if not isinstance(value, dict):
        raise ValueError("Persisted JSON payload must be an object")
    return value


class SessionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, title: str | None = None, session_id: str | None = None) -> SessionRecord:
        now = _utc_now()
        record = SessionRecord(
            id=session_id or _new_id("session"),
            title=title,
            summary=None,
            summary_from_seq=None,
            summary_through_seq=None,
            created_at=now,
            updated_at=now,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, title, summary, summary_from_seq, summary_through_seq,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (record.id, record.title, _timestamp(now), _timestamp(now)),
            )
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def require(self, session_id: str) -> SessionRecord:
        record = self.get(session_id)
        if record is None:
            raise NotFoundError(f"Session not found: {session_id}")
        return record

    def list(self, *, limit: int = 100, offset: int = 0) -> list[SessionRecord]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Invalid pagination")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                ORDER BY updated_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def update_title(self, session_id: str, *, title: str) -> SessionRecord:
        normalized = title.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("Session title must contain 1 to 200 characters")
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (normalized, _timestamp(now), session_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Session not found: {session_id}")
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._from_row(row)

    def delete(self, session_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"Session not found: {session_id}")
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status = 'running'",
                    (session_id,),
                ).fetchone()
                is not None
            ):
                raise ActiveRunExistsError("Cannot delete a Session with a running Run")
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def update_summary(
        self,
        session_id: str,
        *,
        summary: str,
        from_seq: int,
        through_seq: int,
    ) -> SessionRecord:
        if not summary.strip():
            raise ValueError("Summary must not be empty")
        if from_seq < 1 or through_seq < from_seq:
            raise ValueError("Invalid summary sequence range")

        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Session not found: {session_id}")
            if (
                current["summary_through_seq"] is not None
                and through_seq <= current["summary_through_seq"]
            ):
                raise InvalidStateError("Summary coverage must advance monotonically")
            max_seq = connection.execute(
                "SELECT MAX(seq) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            if max_seq is None or through_seq > max_seq:
                raise InvalidStateError("Summary cannot cover messages that do not exist")
            connection.execute(
                """
                UPDATE sessions
                SET summary = ?, summary_from_seq = ?, summary_through_seq = ?, updated_at = ?
                WHERE id = ?
                """,
                (summary, from_seq, through_seq, _timestamp(now), session_id),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            title=row["title"],
            summary=row["summary"],
            summary_from_seq=row["summary_from_seq"],
            summary_through_seq=row["summary_through_seq"],
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
        )


class MessageRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self,
        session_id: str,
        *,
        role: MessageRole,
        kind: MessageKind,
        content: dict[str, Any],
        run_id: str | None = None,
        message_id: str | None = None,
    ) -> MessageRecord:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"Session not found: {session_id}")
            if run_id is not None:
                run = connection.execute(
                    "SELECT session_id FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise NotFoundError(f"Run not found: {run_id}")
                if run["session_id"] != session_id:
                    raise InvalidStateError("Run and message must belong to the same session")

            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            record_id = message_id or _new_id("message")
            connection.execute(
                """
                INSERT INTO messages (
                    id, session_id, run_id, seq, role, kind, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    session_id,
                    run_id,
                    seq,
                    role.value,
                    kind.value,
                    _dump_payload(content),
                    _timestamp(now),
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_timestamp(now), session_id),
            )
        return MessageRecord(
            id=record_id,
            session_id=session_id,
            run_id=run_id,
            seq=seq,
            role=role,
            kind=kind,
            content=content,
            created_at=now,
        )

    def get(self, message_id: str) -> MessageRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_after(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[MessageRecord]:
        if after_seq < 0 or not 1 <= limit <= 10_000:
            raise ValueError("Invalid message range")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (session_id, after_seq, limit),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            seq=row["seq"],
            role=MessageRole(row["role"]),
            kind=MessageKind(row["kind"]),
            content=_load_payload(row["content_json"]),
            created_at=_parse_timestamp(row["created_at"]),
        )


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        session_id: str,
        *,
        user_message_id: str,
        run_id: str | None = None,
    ) -> RunRecord:
        now = _utc_now()
        record_id = run_id or _new_id("run")
        try:
            with self.database.transaction(immediate=True) as connection:
                message = connection.execute(
                    "SELECT session_id, run_id, role FROM messages WHERE id = ?",
                    (user_message_id,),
                ).fetchone()
                if message is None:
                    raise NotFoundError(f"Message not found: {user_message_id}")
                if message["session_id"] != session_id or message["role"] != MessageRole.USER:
                    raise InvalidStateError("A run must start from a user message in its session")
                if message["run_id"] is not None:
                    raise InvalidStateError("The user message is already attached to a run")

                connection.execute(
                    """
                    INSERT INTO runs (
                        id, session_id, status, loop_count, user_message_id,
                        final_message_id, error_code, error_message, started_at, finished_at
                    ) VALUES (?, ?, 'running', 0, ?, NULL, NULL, NULL, ?, NULL)
                    """,
                    (record_id, session_id, user_message_id, _timestamp(now)),
                )
                connection.execute(
                    "UPDATE messages SET run_id = ? WHERE id = ?",
                    (record_id, user_message_id),
                )
        except sqlite3.IntegrityError as error:
            with self.database.connection() as connection:
                active = connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status = 'running'",
                    (session_id,),
                ).fetchone()
            if active is not None:
                raise ActiveRunExistsError(
                    f"Session already has a running run: {session_id}"
                ) from error
            raise

        return RunRecord(
            id=record_id,
            session_id=session_id,
            status=RunStatus.RUNNING,
            loop_count=0,
            user_message_id=user_message_id,
            final_message_id=None,
            error_code=None,
            error_message=None,
            started_at=now,
            finished_at=None,
        )

    def create_with_user_message(
        self,
        session_id: str,
        *,
        text: str,
    ) -> tuple[MessageRecord, RunRecord]:
        """Atomically append one user message and create its running Run."""

        if not text.strip():
            raise ValueError("User message must not be empty")
        now = _utc_now()
        message_id = _new_id("message")
        run_id = _new_id("run")
        try:
            with self.database.transaction(immediate=True) as connection:
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise NotFoundError(f"Session not found: {session_id}")
                active = connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status = 'running'",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise ActiveRunExistsError(f"Session already has a running run: {session_id}")

                message_seq = _next_message_seq(connection, session_id)
                content = {"text": text}
                connection.execute(
                    """
                    INSERT INTO messages (
                        id, session_id, run_id, seq, role, kind, content_json, created_at
                    ) VALUES (?, ?, ?, ?, 'user', 'message', ?, ?)
                    """,
                    (
                        message_id,
                        session_id,
                        run_id,
                        message_seq,
                        _dump_payload(content),
                        _timestamp(now),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, session_id, status, loop_count, user_message_id,
                        final_message_id, error_code, error_message, started_at, finished_at
                    ) VALUES (?, ?, 'running', 0, ?, NULL, NULL, NULL, ?, NULL)
                    """,
                    (run_id, session_id, message_id, _timestamp(now)),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (_timestamp(now), session_id),
                )
        except sqlite3.IntegrityError as error:
            with self.database.connection() as connection:
                active = connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status = 'running'",
                    (session_id,),
                ).fetchone()
            if active is not None:
                raise ActiveRunExistsError(
                    f"Session already has a running run: {session_id}"
                ) from error
            raise

        message = MessageRecord(
            id=message_id,
            session_id=session_id,
            run_id=run_id,
            seq=message_seq,
            role=MessageRole.USER,
            kind=MessageKind.MESSAGE,
            content=content,
            created_at=now,
        )
        run = RunRecord(
            id=run_id,
            session_id=session_id,
            status=RunStatus.RUNNING,
            loop_count=0,
            user_message_id=message_id,
            final_message_id=None,
            error_code=None,
            error_message=None,
            started_at=now,
            finished_at=None,
        )
        return message, run

    def get(self, run_id: str) -> RunRecord | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._from_row(row) if row is not None else None

    def require(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record is None:
            raise NotFoundError(f"Run not found: {run_id}")
        return record

    def list_running(self) -> list[RunRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at, id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_for_session(self, session_id: str) -> list[RunRecord]:
        """Return runs in conversation order, based on their user message sequence."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT runs.*
                FROM runs
                JOIN messages ON messages.id = runs.user_message_id
                WHERE runs.session_id = ?
                ORDER BY messages.seq ASC, runs.id ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def increment_loop(self, run_id: str) -> RunRecord:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET loop_count = loop_count + 1
                WHERE id = ? AND status = 'running'
                """,
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidStateError(f"Run is missing or not running: {run_id}")
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._from_row(row)

    def complete(self, run_id: str, *, final_message_id: str) -> RunRecord:
        finished_at = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError(f"Run not found: {run_id}")
            if run["status"] != RunStatus.RUNNING:
                raise InvalidStateError(f"Run is not running: {run_id}")
            message = connection.execute(
                "SELECT session_id, run_id, role FROM messages WHERE id = ?",
                (final_message_id,),
            ).fetchone()
            if (
                message is None
                or message["session_id"] != run["session_id"]
                or message["run_id"] != run_id
                or message["role"] != MessageRole.ASSISTANT
            ):
                raise InvalidStateError("Final message must be an assistant message from this run")
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed', final_message_id = ?, finished_at = ?
                WHERE id = ?
                """,
                (final_message_id, _timestamp(finished_at), run_id),
            )
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._from_row(row)

    def fail(self, run_id: str, *, error_code: str, error_message: str) -> RunRecord:
        if not error_code:
            raise ValueError("error_code must not be empty")
        finished_at = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = 'failed', error_code = ?, error_message = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (error_code, error_message, _timestamp(finished_at), run_id),
            )
            if cursor.rowcount != 1:
                raise InvalidStateError(f"Run is missing or not running: {run_id}")
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._from_row(row)

    def complete_with_final_answer(
        self,
        run_id: str,
        *,
        text: str,
    ) -> tuple[RunRecord, MessageRecord, StreamEventRecord]:
        """Atomically save the answer, complete the Run, trace it, and append its SSE terminal."""

        if not text.strip():
            raise ValueError("Final answer must not be empty")
        now = _utc_now()
        message_id = _new_id("message")
        with self.database.transaction(immediate=True) as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError(f"Run not found: {run_id}")
            if run["status"] != RunStatus.RUNNING:
                raise InvalidStateError(f"Run is not running: {run_id}")
            message_seq = _next_message_seq(connection, run["session_id"])
            content = {"text": text}
            connection.execute(
                """
                INSERT INTO messages (
                    id, session_id, run_id, seq, role, kind, content_json, created_at
                ) VALUES (?, ?, ?, ?, 'assistant', 'message', ?, ?)
                """,
                (
                    message_id,
                    run["session_id"],
                    run_id,
                    message_seq,
                    _dump_payload(content),
                    _timestamp(now),
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed', final_message_id = ?, finished_at = ?
                WHERE id = ?
                """,
                (message_id, _timestamp(now), run_id),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_timestamp(now), run["session_id"]),
            )
            terminal = _insert_stream_event(
                connection,
                run_id,
                PendingStreamEvent(
                    event_type="run.completed",
                    payload={"run_id": run_id, "message_id": message_id},
                ),
                now,
            )
            connection.execute(
                """
                INSERT INTO traces (
                    run_id, step_no, event_type, payload_json, duration_ms, created_at
                ) VALUES (?, ?, 'run.completed', ?, NULL, ?)
                """,
                (
                    run_id,
                    run["loop_count"],
                    _dump_payload({"message_id": message_id}),
                    _timestamp(now),
                ),
            )
            run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

        message = MessageRecord(
            id=message_id,
            session_id=run["session_id"],
            run_id=run_id,
            seq=message_seq,
            role=MessageRole.ASSISTANT,
            kind=MessageKind.MESSAGE,
            content=content,
            created_at=now,
        )
        return self._from_row(run_row), message, terminal

    def fail_with_error(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> tuple[RunRecord, MessageRecord, StreamEventRecord]:
        """Atomically save a safe error, fail the Run, trace it, and append its SSE terminal."""

        if not error_code or not error_message:
            raise ValueError("Run error code and message must not be empty")
        now = _utc_now()
        message_id = _new_id("message")
        with self.database.transaction(immediate=True) as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError(f"Run not found: {run_id}")
            if run["status"] != RunStatus.RUNNING:
                raise InvalidStateError(f"Run is not running: {run_id}")
            message_seq = _next_message_seq(connection, run["session_id"])
            content = {"code": error_code, "message": error_message}
            connection.execute(
                """
                INSERT INTO messages (
                    id, session_id, run_id, seq, role, kind, content_json, created_at
                ) VALUES (?, ?, ?, ?, 'assistant', 'system_error', ?, ?)
                """,
                (
                    message_id,
                    run["session_id"],
                    run_id,
                    message_seq,
                    _dump_payload(content),
                    _timestamp(now),
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', error_code = ?, error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (error_code, error_message, _timestamp(now), run_id),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_timestamp(now), run["session_id"]),
            )
            terminal = _insert_stream_event(
                connection,
                run_id,
                PendingStreamEvent(
                    event_type="run.failed",
                    payload={
                        "run_id": run_id,
                        "error_code": error_code,
                        "message": error_message,
                    },
                ),
                now,
            )
            connection.execute(
                """
                INSERT INTO traces (
                    run_id, step_no, event_type, payload_json, duration_ms, created_at
                ) VALUES (?, ?, 'run.failed', ?, NULL, ?)
                """,
                (
                    run_id,
                    run["loop_count"],
                    _dump_payload({"error_code": error_code, "message": error_message}),
                    _timestamp(now),
                ),
            )
            run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

        message = MessageRecord(
            id=message_id,
            session_id=run["session_id"],
            run_id=run_id,
            seq=message_seq,
            role=MessageRole.ASSISTANT,
            kind=MessageKind.SYSTEM_ERROR,
            content=content,
            created_at=now,
        )
        return self._from_row(run_row), message, terminal

    def mark_running_failed(self, *, error_code: str, error_message: str) -> int:
        """Mark stale runs failed during process startup recovery."""

        if not error_code:
            raise ValueError("error_code must not be empty")
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = 'failed', error_code = ?, error_message = ?, finished_at = ?
                WHERE status = 'running'
                """,
                (error_code, error_message, _timestamp(_utc_now())),
            )
        return cursor.rowcount

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            session_id=row["session_id"],
            status=RunStatus(row["status"]),
            loop_count=row["loop_count"],
            user_message_id=row["user_message_id"],
            final_message_id=row["final_message_id"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=_parse_timestamp(row["started_at"]),
            finished_at=_parse_timestamp(row["finished_at"]),
        )


class TraceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self,
        run_id: str,
        *,
        step_no: int,
        event_type: str,
        payload: dict[str, Any],
        duration_ms: int | None = None,
    ) -> TraceRecord:
        if step_no < 0 or not event_type or (duration_ms is not None and duration_ms < 0):
            raise ValueError("Invalid trace event")
        now = _utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO traces (
                    run_id, step_no, event_type, payload_json, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_no,
                    event_type,
                    _dump_payload(payload),
                    duration_ms,
                    _timestamp(now),
                ),
            )
            record_id = cursor.lastrowid
        return TraceRecord(
            id=record_id,
            run_id=run_id,
            step_no=step_no,
            event_type=event_type,
            payload=payload,
            duration_ms=duration_ms,
            created_at=now,
        )

    def list_for_run(self, run_id: str) -> list[TraceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM traces WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TraceRecord:
        return TraceRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_no=row["step_no"],
            event_type=row["event_type"],
            payload=_load_payload(row["payload_json"]),
            duration_ms=row["duration_ms"],
            created_at=_parse_timestamp(row["created_at"]),
        )


class StreamEventRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append_batch(
        self,
        run_id: str,
        events: list[PendingStreamEvent],
    ) -> list[StreamEventRecord]:
        if not events:
            return []
        now = _utc_now()
        records: list[StreamEventRecord] = []
        with self.database.transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise NotFoundError(f"Run not found: {run_id}")
            next_seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM stream_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            for event in events:
                records.append(
                    _insert_stream_event(connection, run_id, event, now, next_seq=next_seq)
                )
                next_seq += 1
        return records

    def list_after_id(
        self,
        run_id: str,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[StreamEventRecord]:
        if after_id < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid stream event range")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM stream_events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_by_event_types(
        self,
        run_id: str,
        event_types: tuple[str, ...],
    ) -> list[StreamEventRecord]:
        """Return all matching events for one bounded Run in durable order."""

        if not event_types:
            return []
        placeholders = ", ".join("?" for _ in event_types)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM stream_events
                WHERE run_id = ? AND event_type IN ({placeholders})
                ORDER BY id ASC
                """,
                (run_id, *event_types),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> StreamEventRecord:
        return StreamEventRecord(
            id=row["id"],
            run_id=row["run_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            payload=_load_payload(row["payload_json"]),
            source_seq=row["source_seq"],
            created_at=_parse_timestamp(row["created_at"]),
        )


def _next_message_seq(connection: sqlite3.Connection, session_id: str) -> int:
    return connection.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]


def _insert_stream_event(
    connection: sqlite3.Connection,
    run_id: str,
    event: PendingStreamEvent,
    created_at: datetime,
    *,
    next_seq: int | None = None,
) -> StreamEventRecord:
    seq = next_seq
    if seq is None:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM stream_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    cursor = connection.execute(
        """
        INSERT INTO stream_events (
            run_id, seq, event_type, payload_json, source_seq, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            seq,
            event.event_type,
            _dump_payload(event.payload),
            event.source_seq,
            _timestamp(created_at),
        ),
    )
    return StreamEventRecord(
        id=cursor.lastrowid,
        run_id=run_id,
        seq=seq,
        event_type=event.event_type,
        payload=event.payload,
        source_seq=event.source_seq,
        created_at=created_at,
    )
