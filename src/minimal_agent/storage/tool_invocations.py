"""Durable logical tool calls and atomic tool message boundaries."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from minimal_agent.schemas import ToolInvocationRecord, ToolInvocationStatus
from minimal_agent.storage.database import Database
from minimal_agent.storage.repositories import InvalidStateError, NotFoundError


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ToolInvocationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_with_tool_call(
        self,
        *,
        run_id: str,
        session_id: str,
        step_no: int,
        provider_call_id: str,
        tool_name: str,
        arguments_json: str,
        retry_semantics: str,
        idempotency_key: str | None,
        tool_call_content: dict[str, Any],
    ) -> ToolInvocationRecord:
        now = _now()
        invocation_id = f"toolinv_{uuid4().hex}"
        message_id = f"message_{uuid4().hex}"
        digest = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
        with self.database.transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT session_id, status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise NotFoundError(f"Run not found: {run_id}")
            if run["session_id"] != session_id or run["status"] != "running":
                raise InvalidStateError("Tool invocation requires its running session Run")
            existing = connection.execute(
                "SELECT * FROM tool_invocations WHERE run_id = ? AND provider_call_id = ?",
                (run_id, provider_call_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["tool_name"] != tool_name
                    or existing["arguments_hash"] != digest
                    or existing["retry_semantics"] != retry_semantics
                ):
                    raise InvalidStateError("Provider call id was reused for a different tool call")
                return self._from_row(existing)
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO messages (
                    id, session_id, run_id, seq, role, kind, content_json, created_at
                ) VALUES (?, ?, ?, ?, 'assistant', 'tool_call', ?, ?)
                """,
                (message_id, session_id, run_id, seq, _dump(tool_call_content), _stamp(now)),
            )
            connection.execute(
                """
                INSERT INTO tool_invocations (
                    id, run_id, session_id, step_no, provider_call_id, tool_name,
                    arguments_json, arguments_hash, idempotency_key, retry_semantics,
                    status, attempt_count, result_json, error_json,
                    tool_call_message_id, result_message_id, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL,
                          ?, NULL, ?, ?, NULL)
                """,
                (
                    invocation_id,
                    run_id,
                    session_id,
                    step_no,
                    provider_call_id,
                    tool_name,
                    arguments_json,
                    digest,
                    idempotency_key,
                    retry_semantics,
                    message_id,
                    _stamp(now),
                    _stamp(now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        return self._from_row(row)

    def get(self, invocation_id: str) -> ToolInvocationRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def require(self, invocation_id: str) -> ToolInvocationRecord:
        record = self.get(invocation_id)
        if record is None:
            raise NotFoundError(f"Tool invocation not found: {invocation_id}")
        return record

    def latest_for_run(self, run_id: str) -> ToolInvocationRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM tool_invocations
                WHERE run_id = ? ORDER BY step_no DESC, created_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def start_attempt(self, invocation_id: str) -> ToolInvocationRecord:
        now = _now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'pending', attempt_count = attempt_count + 1,
                    error_json = NULL, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'unknown')
                """,
                (_stamp(now), invocation_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Tool invocation not found: {invocation_id}")
                raise InvalidStateError("Completed tool invocation cannot start another attempt")
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        return self._from_row(row)

    def mark_unknown(self, invocation_id: str, *, error: dict[str, Any]) -> ToolInvocationRecord:
        now = _now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'unknown', error_json = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (_dump(error), _stamp(now), invocation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidStateError("Only a pending invocation can become unknown")
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        return self._from_row(row)

    def complete_with_tool_result(self, invocation_id: str, result: Any) -> ToolInvocationRecord:
        now = _now()
        result_payload = result.model_dump(mode="json")
        status = "succeeded" if result.ok else "failed"
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Tool invocation not found: {invocation_id}")
            if row["status"] in ("succeeded", "failed"):
                return self._from_row(row)
            message_id = f"message_{uuid4().hex}"
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (row["session_id"],),
            ).fetchone()[0]
            content = {
                "call_id": row["provider_call_id"],
                "invocation_id": invocation_id,
                "result": result_payload,
            }
            connection.execute(
                """
                INSERT INTO messages (
                    id, session_id, run_id, seq, role, kind, content_json, created_at
                ) VALUES (?, ?, ?, ?, 'tool', 'tool_result', ?, ?)
                """,
                (
                    message_id,
                    row["session_id"],
                    row["run_id"],
                    seq,
                    _dump(content),
                    _stamp(now),
                ),
            )
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = ?, result_json = ?, error_json = NULL,
                    result_message_id = ?, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    _dump(result_payload),
                    message_id,
                    _stamp(now),
                    _stamp(now),
                    invocation_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM tool_invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        return self._from_row(updated)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ToolInvocationRecord:
        return ToolInvocationRecord(
            id=row["id"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            step_no=row["step_no"],
            provider_call_id=row["provider_call_id"],
            tool_name=row["tool_name"],
            arguments_json=row["arguments_json"],
            arguments_hash=row["arguments_hash"],
            idempotency_key=row["idempotency_key"],
            retry_semantics=row["retry_semantics"],
            status=ToolInvocationStatus(row["status"]),
            attempt_count=row["attempt_count"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            tool_call_message_id=row["tool_call_message_id"],
            result_message_id=row["result_message_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
        )
