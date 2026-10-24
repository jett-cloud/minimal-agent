from collections.abc import Iterator

import pytest

from minimal_agent.storage import Database


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
