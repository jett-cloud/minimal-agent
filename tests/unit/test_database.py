import sqlite3

import pytest

from minimal_agent.storage import Database


def test_database_initializes_schema_and_connection_pragmas(database: Database) -> None:
    database.initialize()
    with database.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        schema_versions = connection.execute("SELECT version FROM schema_meta").fetchall()

    assert {"sessions", "messages", "runs", "traces", "stream_events"} <= tables
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert busy_timeout == 5000
    assert [row[0] for row in schema_versions] == [1]


def test_foreign_keys_are_enforced(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO messages (
                id, session_id, run_id, seq, role, kind, content_json, created_at
            ) VALUES ('message_1', 'missing', NULL, 1, 'user', 'message', '{}', 'now')
            """
        )


def test_transaction_rolls_back_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError, match="stop"), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                id, title, summary, summary_from_seq, summary_through_seq,
                created_at, updated_at
            ) VALUES ('session_rollback', NULL, NULL, NULL, NULL, 'now', 'now')
            """
        )
        raise RuntimeError("stop")

    with database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 'session_rollback'"
        ).fetchone()[0]

    assert count == 0
